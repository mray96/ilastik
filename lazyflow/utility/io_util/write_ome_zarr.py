import dataclasses
import logging
import math
from collections import OrderedDict as ODict
from functools import partial
from typing import List, Tuple, Dict, Optional, OrderedDict

import numpy
import zarr
from zarr.storage import FSStore

from ilastik import __version__ as ilastik_version
from lazyflow.operators import OpReorderAxes
from lazyflow.roi import determineBlockShape, roiFromShape, roiToSlice
from lazyflow.slot import Slot
from lazyflow.utility import OrderedSignal, PathComponents, BigRequestStreamer
from lazyflow.utility.io_util.OMEZarrStore import OME_ZARR_V_0_4_KWARGS

logger = logging.getLogger(__name__)

Shape = Tuple[int, ...]
TaggedShape = OrderedDict[str, int]  # axis: size
OrderedScaling = OrderedDict[str, float]  # axis: scale

SPATIAL_AXES = ["z", "y", "x"]


@dataclasses.dataclass
class ImageMetadata:
    path: str
    scale: OrderedScaling
    translation: Dict[str, float]


def _get_chunk_shape(image_source_slot: Slot) -> Shape:
    """Determine chunk shape for OME-Zarr storage based on image source slot.
    Chunk size is 1 for t and c, and determined by ilastik default rules for zyx, with a target of 512KB per chunk."""
    dtype = image_source_slot.meta.dtype
    if isinstance(dtype, numpy.dtype):  # Extract raw type class
        dtype = dtype.type
    dtype_bytes = dtype().nbytes
    tagged_maxshape = image_source_slot.meta.getTaggedShape()
    tagged_maxshape["t"] = 1
    tagged_maxshape["c"] = 1
    chunk_shape = determineBlockShape(list(tagged_maxshape.values()), 512_000.0 / dtype_bytes)  # 512KB chunk size
    return chunk_shape


def _get_scalings(
    original_tagged_shape: TaggedShape, chunk_shape: Shape, min_length: Optional[int]
) -> List[OrderedScaling]:
    """
    Computes scaling "factors".
    Technically they are divisors for the shape (factor 2.0 means half the shape).
    Downscaling is done by a factor of 2 in all spatial dimensions until:
    - the dataset would be less than 4 x chunk size (2MiB)
    - an axis that started non-singleton would become singleton
    - the largest axis would be smaller than min_length (if defined).
    Returns list of scaling factor dicts by axis, starting with original scale.
    The scaling level that meets one of the exit conditions is excluded.
    Raises if more than 20 scales are computed (sanity).
    """
    assert len(chunk_shape) == len(original_tagged_shape), "Chunk shape and tagged shape must have same length"
    original_scale = ODict([(a, 1.0) for a in original_tagged_shape.keys()])
    scalings = [original_scale]
    sanity_limit = 20
    for i in range(sanity_limit):
        if i == sanity_limit:
            raise ValueError(f"Too many scales computed, limit={sanity_limit}. Please report this to the developers.")
        new_scaling = ODict(
            [
                (a, 2.0 ** (i + 1)) if a in SPATIAL_AXES and original_tagged_shape[a] > 1 else (a, 1.0)
                for a in original_tagged_shape.keys()
            ]
        )
        new_shape = _scale_tagged_shape(original_tagged_shape, new_scaling)
        if (
            _is_less_than_4_chunks(new_shape, chunk_shape)
            or _reduces_any_axis_to_singleton(new_shape.values(), original_tagged_shape.values())
            or (min_length and max(new_shape.values()) < min_length)
        ):
            break
        scalings.append(new_scaling)
    return scalings


def _reduces_any_axis_to_singleton(new_shape: Shape, original_shape: Shape):
    return any(new <= 1 < orig for new, orig in zip(new_shape, original_shape))


def _is_less_than_4_chunks(new_shape: TaggedShape, chunk_shape: Shape):
    spatial_shape = [s for a, s in new_shape.items() if a in SPATIAL_AXES]
    return numpy.prod(spatial_shape) < 4 * numpy.prod(chunk_shape)


def _scale_tagged_shape(original_tagged_shape: TaggedShape, scaling: OrderedScaling) -> TaggedShape:
    assert all(s > 0 for s in scaling.values()), f"Invalid scaling: {scaling}"
    return ODict(
        [
            (a, _round_like_scaling_method(s / scaling[a]) if a in scaling else s)
            for a, s in original_tagged_shape.items()
        ]
    )


def _round_like_scaling_method(value: float) -> int:
    """For calculating scaled shape after applying the scaling method.
    Different scaling methods might round differently, so we need to match that."""
    # Currently the only rounding method is 2-step indexing of numpy array, which always rounds up
    # numpy.ones(7)[::2].shape == (4,)
    return math.ceil(value)


def _apply_scaling_method(
    data: numpy.typing.NDArray, current_block_roi: Tuple[List[int], List[int]], scaling: OrderedScaling
) -> Tuple[numpy.typing.NDArray, Tuple[List[int], List[int]]]:
    """Downscale data by applying scaling factors to spatial dimensions.
    Ordering of `data.shape`, scaling and current_block_roi must match.
    Needs to know block roi to determine position of the scaled block within the total scaled image.
    Returns scaled data and scaled roi because the roi must be adjusted for blockwise rounding.
    """
    scaling_int = [int(s) for s in scaling.values()]
    starts = current_block_roi[0]
    # Specific to downsampling, where scale = step size.
    # When scale=5, the pixels that should be included in the final result are at 0, 5, 10, 15, ...
    # If e.g. start=22 for this block, the block must internally add crop=3,
    # so that it globally starts at 25: 22 + 3 = 25, where 3 = 5 - (22 % 5)
    block_start_crops = [
        (scale - (start % scale)) if start % scale > 0 else 0 for start, scale in zip(starts, scaling_int)
    ]
    crop_and_downsample_slicing = tuple(slice(crop, None, scale) for crop, scale in zip(block_start_crops, scaling_int))
    scaled_starts = [_round_like_scaling_method(start / scale) for start, scale in zip(starts, scaling_int)]
    scaled_stops = [_round_like_scaling_method(stop / scale) for stop, scale in zip(current_block_roi[1], scaling_int)]
    scaled_roi = (scaled_starts, scaled_stops)
    return data[crop_and_downsample_slicing], scaled_roi


def _compute_and_write_scales(
    export_path: str, image_source_slot: Slot, progress_signal: OrderedSignal, min_length: Optional[int]
) -> List[ImageMetadata]:
    pc = PathComponents(export_path)
    external_path = pc.externalPath
    internal_path = pc.internalPath.lstrip("/") if pc.internalPath else None
    store = FSStore(external_path, mode="w", **OME_ZARR_V_0_4_KWARGS)
    chunk_shape = _get_chunk_shape(image_source_slot)

    scalings = _get_scalings(image_source_slot.meta.getTaggedShape(), chunk_shape, min_length)
    zarrays = []
    meta = []
    for i, scaling in enumerate(scalings):
        scale_path = f"{internal_path}/s{i}" if internal_path else f"s{i}"
        scaled_shape = _scale_tagged_shape(image_source_slot.meta.getTaggedShape(), scaling).values()
        zarrays.append(
            zarr.creation.empty(
                scaled_shape, store=store, path=scale_path, chunks=chunk_shape, dtype=image_source_slot.meta.dtype
            )
        )
        meta.append(ImageMetadata(scale_path, scaling, {}))

    def scale_and_write_block(scalings_, zarrays_, roi, data):
        for i_, scaling_ in enumerate(scalings_):
            if i_ > 0:
                logger.info(f"Scale {i_}: Applying {scaling_=} to {roi=}")
                scaled_data, scaled_roi = _apply_scaling_method(data, roi, scaling_)
                slicing = roiToSlice(*scaled_roi)
            else:
                slicing = roiToSlice(*roi)
                scaled_data = data
            logger.info(f"Scale {i_}: Writing data with shape={scaled_data.shape} to {slicing=}")
            zarrays_[i_][slicing] = scaled_data

    requester = BigRequestStreamer(image_source_slot, roiFromShape(image_source_slot.meta.shape))
    requester.resultSignal.subscribe(partial(scale_and_write_block, scalings, zarrays))
    requester.progressSignal.subscribe(progress_signal)
    requester.execute()

    return meta


def _write_ome_zarr_and_ilastik_metadata(
    export_path: str, multiscale_metadata: List[ImageMetadata], ilastik_meta: Dict
):
    pc = PathComponents(export_path)
    external_path = pc.externalPath
    multiscale_name = pc.internalPath.lstrip("/") if pc.internalPath else None
    ilastik_signature = {"name": "ilastik", "version": ilastik_version, "ome_zarr_exporter_version": 1}
    axis_types = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}
    axes = [{"name": tag.key, "type": axis_types[tag.key]} for tag in ilastik_meta["axistags"]]
    datasets = [
        {
            "path": image.path,
            "coordinateTransformations": [
                {"type": "scale", "scale": [image.scale[tag.key] for tag in ilastik_meta["axistags"]]}
            ],
        }
        for image in multiscale_metadata
    ]
    ome_zarr_multiscale_meta = {"_creator": ilastik_signature, "version": "0.4", "axes": axes, "datasets": datasets}
    if multiscale_name:
        ome_zarr_multiscale_meta["name"] = multiscale_name
    store = FSStore(external_path, mode="w", **OME_ZARR_V_0_4_KWARGS)
    root = zarr.group(store, overwrite=False)
    root.attrs["multiscales"] = [ome_zarr_multiscale_meta]
    for image in multiscale_metadata:
        za = zarr.Array(store, path=image.path)
        za.attrs["axistags"] = ilastik_meta["axistags"].toJSON()
        if ilastik_meta["display_mode"]:
            za.attrs["display_mode"] = ilastik_meta["display_mode"]
        if ilastik_meta["drange"]:
            za.attrs["drange"] = ilastik_meta["drange"]


def write_ome_zarr(
    export_path: str,
    image_source_slot: Slot,
    progress_signal: OrderedSignal,
    min_length: Optional[int] = None,
):
    op_reorder = OpReorderAxes(parent=image_source_slot.operator)
    op_reorder.AxisOrder.setValue("tczyx")
    try:
        op_reorder.Input.connect(image_source_slot)
        image_source = op_reorder.Output
        progress_signal(25)
        ome_zarr_meta = _compute_and_write_scales(export_path, image_source, progress_signal, min_length)
        progress_signal(95)
        _write_ome_zarr_and_ilastik_metadata(
            export_path,
            ome_zarr_meta,
            {
                "axistags": op_reorder.Output.meta.axistags,
                "display_mode": image_source_slot.meta.get("display_mode"),
                "drange": image_source_slot.meta.get("drange"),
            },
        )
    finally:
        op_reorder.cleanUp()
