###############################################################################
#   lazyflow: data flow based lazy parallel computation framework
#
#       Copyright (C) 2011-2017, the ilastik developers
#                                <team@ilastik.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the Lesser GNU General Public License
# as published by the Free Software Foundation; either version 2.1
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# See the files LICENSE.lgpl2 and LICENSE.lgpl3 for full text of the
# GNU Lesser General Public License version 2.1 and 3 respectively.
# This information is also available on the ilastik web site at:
#          http://ilastik.org/license/
###############################################################################
import os
import z5py

from lazyflow.graph import Operator, InputSlot, OutputSlot
from lazyflow.operators.generic import OpMultiArrayStacker
from lazyflow.operators.ioOperators.opStreamingN5Reader import OpStreamingN5Reader
from lazyflow.utility.pathHelpers import PathComponents, globHdf5N5

import logging
logger = logging.getLogger(__name__)


class OpStreamingN5SequenceReaderS(Operator):
    """
    Imports a sequence of (ND) volumes inside one hdf5 file into a single volume (ND+1)

    The 'S' at the end of the file name implies that this class handles multiple
    volumes in a single file.

    :param globstring: A glob string as defined by the glob module. We
        also support the following special extension to globstring
        syntax: A single string can hold a *list* of globstrings.
        The delimiter that separates the globstrings in the list is
        OS-specific via os.path.pathsep.

        For example, on Linux the pathsep is':', so

            '/a/b/c.txt:/d/e/f.txt:../g/i/h.txt'

        is parsed as

            ['/a/b/c.txt', '/d/e/f.txt', '../g/i/h.txt']

    """
    GlobString = InputSlot()
    # The project hdf5 File object (already opened)
    SequenceAxis = InputSlot(optional=True)  # The axis to stack across.
    OutputImage = OutputSlot()

    class WrongFileTypeError(Exception):
        def __init__(self, globString):
            self.filename = globString
            self.msg = "File is not a N5: {}".format(globString)
            super(OpStreamingHd5SequenceReaderS.WrongFileTypeError, self).__init__(self.msg)

    class NotTheSameFileError(Exception):
        def __init__(self, globString):
            self.globString = globString
            self.msg = "Glob string encompasses more than one N5 file: {}".format(globString)
            super(OpStreamingN5SequenceReaderS.NotTheSameFileError, self).__init__(self.msg)

    class NoInternalPlaceholderError(Exception):
        def __init__(self, globString):
            self.globString = globString
            self.msg = "Glob string does not contain a placeholder: {}".format(globString)
            super(OpStreamingN5SequenceReaderS.NoInternalPlaceholderError, self).__init__(self.msg)

    class ExternalPlaceholderError(Exception):
        def __init__(self, globString):
            self.globString = globString
            self.msg = ("Glob string does contains an external placeholder "
                        "(not supported!): {}".format(globString))
            super(OpStreamingN5SequenceReaderS.ExternalPlaceholderError, self).__init__(self.msg)

    def __init__(self, *args, **kwargs):
        super(OpStreamingN5SequenceReaderS, self).__init__(*args, **kwargs)
        self._n5File = None
        self._readers = []
        self._opStacker = OpMultiArrayStacker(parent=self)
        self._opStacker.AxisIndex.setValue(0)

    def cleanUp(self):
        self._opStacker.Images.resize(0)
        for opReader in self._readers:
            opReader.cleanUp()
        if self._n5File is not None:
            assert isinstance(self._n5File, z5py.N5File), (
                "_n5File should not be of any other type")
            self._n5File.close()

        super(OpStreamingN5SequenceReaderS, self).cleanUp()

    def setupOutputs(self):
        pcs = PathComponents(self.GlobString.value.split(os.path.pathsep)[0])
        self._n5File = z5py.N5File(pcs.externalPath, mode='r')
        self.checkGlobString(self.GlobString.value)
        file_paths = self.expandGlobStrings(self._n5File, self.GlobString.value)

        num_files = len(file_paths)
        if num_files == 0:
            self.OutputImage.disconnect()
            self.OutputImage.meta.NOTREADY = True
            return

        self.OutputImage.connect(self._opStacker.Output)
        # Get slice axes from first image
        try:
            opFirstImg = OpStreamingN5Reader(parent=self)
            opFirstImg.InternalPath.setValue(file_paths[0])
            opFirstImg.n5File.setValue(self._n5File)
            slice_axes = opFirstImg.OutputImage.meta.getAxisKeys()
            opFirstImg.cleanUp()
        except RuntimeError as e:
            logger.error(str(e))
            raise OpStreamingN5SequenceReaderS.FileOpenError(file_paths[0])

        # Use given new axis or try to do something sensible
        if self.SequenceAxis.ready():
            new_axis = self.SequenceAxis.value
            assert len(new_axis) == 1
            assert new_axis in 'tzyxc'
        else:
            # Try to pick an axis that doesn't already exist in each volume
            for new_axis in 'tzc0':
                if new_axis not in slice_axes:
                    break

            if new_axis == '0':
                # All axes used already.
                # Stack across first existing axis
                new_axis = slice_axes[0]

        self._opStacker.Images.resize(0)
        self._opStacker.Images.resize(num_files)
        self._opStacker.AxisFlag.setValue(new_axis)

        for opReader in self._readers:
            opReader.cleanUp()

        self._readers = []
        for filename, stacker_slot in zip(file_paths, self._opStacker.Images):
            opReader = OpStreamingN5Reader(parent=self)
            try:
                opReader.InternalPath.setValue(filename)
                opReader.n5File.setValue(self._n5File)
            except RuntimeError as e:
                logger.error(str(e))
                raise OpStreamingN5SequenceReaderS.FileOpenError(file_paths[0])
            else:
                stacker_slot.connect(opReader.OutputImage)
                self._readers.append(opReader)

    def propagateDirty(self, slot, subindex, roi):
        if slot == self.GlobString or slot == self.SequenceAxis:
            self.OutputImage.setDirty(slice(None))

    @staticmethod
    def expandGlobStrings(n5File, globStrings):
        """Matches a list of globStrings to internal paths of files

        Args:
            n5File: z5py.N5File object, or path(string). If a string is given,
              the file is opened and closed in this method.
            globStrings: string. glob or path strings delimited by os.pathsep

        Returns:
            List of internal paths matching the globstrings that were found in
            the provided z5py.N5File object
        """
        if not isinstance(n5File, z5py.N5File):
            with z5py.N5File(n5File, mode='r') as f:
                ret = OpStreamingN5SequenceReaderS.expandGlobStrings(f, globStrings)
            return ret

        ret = []
        # Parse list into separate globstrings and combine them
        for globString in globStrings.split(os.path.pathsep):
            s = globString.strip()
            components = PathComponents(s)
            ret += sorted(
                globHdf5N5(
                    n5File, components.internalPath.lstrip('/')))
        return ret

    @staticmethod
    def checkGlobString(globString):
        """Checks whether globString is valid for this class

        Rules for globString:
            * must only contain one distinct external path
            * multiple internal paths, or placeholder '*' must be contained

        Args:
            globString (string): String, one or multiple paths separated with
              os.path.pathsep and possibly containing '*' as a placeholder.

        Raises:
            OpStreamingN5SequenceReaderS.ExternalPlaceholderError: External
              placeholders are not supported.
            OpStreamingN5SequenceReaderS.NoInternalPlaceholderError: This
              exception is raised if only a single path is provided ->
              OpStreamingN5Reader should be used in this case.
            OpStreamingN5SequenceReaderS.NotTheSameFileError: if multiple
              N5 files are (possibly) referenced in the globstring, this
              Exception is raised -> OpStreamingN5SequenceReaderM should
              be used in this case.
            OpStreamingN5SequenceReaderS.WrongFileTypeError:If file-
                extensions are not among the known N5 extensions, this error is
                raised (see OpStreamingN5Reader.N5EXTS)
        """
        pathStrings = globString.split(os.path.pathsep)

        pathComponents = [PathComponents(p.strip()) for p in pathStrings]
        assert len(pathComponents) > 0

        if not all(p.extension in OpStreamingN5Reader.N5EXTS
                   for p in pathComponents):
            raise OpStreamingN5SequenceReaderS.WrongFileTypeError(globString)

        if len(pathComponents) == 1:
            if pathComponents[0].internalPath is None:
                raise OpStreamingN5SequenceReaderS.NoInternalPlaceholderError(globString)
            if '*' not in pathComponents[0].internalPath:
                raise OpStreamingN5SequenceReaderS.NoInternalPlaceholderError(globString)
            if '*' in pathComponents[0].externalPath:
                raise OpStreamingN5SequenceReaderS.ExternalPlaceholderError(globString)
        else:
            sameExternal = all(pathComponents[0].externalPath == x.externalPath
                               for x in pathComponents[1::])
            if sameExternal is not True:
                raise OpStreamingN5SequenceReaderS.NotTheSameFileError(globString)
            if '*' in pathComponents[0].externalPath:
                raise OpStreamingN5SequenceReaderS.ExternalPlaceholderError(globString)
