"""
Depending on the demand this might get reworked into a real "browser". Right now
this will only be used to punch in the url and do some validation. Naming of the
file is just to reflect the similar function as dvidDataSelectionBrowser.

Todos:
  - check whether can me somehow merged with dvidDataSelctionBrowser

"""

import logging
import pathlib

from requests.exceptions import SSLError, ConnectionError
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
)

from lazyflow.utility import isUrl
from lazyflow.utility.io_util.OMEZarrStore import OMEZarrStore
from lazyflow.utility.io_util.RESTfulPrecomputedChunkedVolume import RESTfulPrecomputedChunkedVolume

logger = logging.getLogger(__name__)


class MultiscaleDatasetBrowser(QDialog):
    def __init__(self, history=None, parent=None):
        super().__init__(parent)
        self._history = history or []
        self.selected_url = None  # Return value read by the caller after the dialog is closed

        self.setup_ui()

    def setup_ui(self):
        self.setMinimumSize(800, 200)
        self.setWindowTitle("Select Multiscale Source")
        main_layout = QVBoxLayout()

        description = QLabel(self)
        description.setText('Enter path or URL and click "Check".')
        main_layout.addWidget(description)

        self.combo = QComboBox(self)
        self.combo.setEditable(True)
        self.combo.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Minimum)
        self.combo.addItem("")
        for item in self._history:
            self.combo.addItem(item)

        combo_label = QLabel(self)
        combo_label.setText("Dataset address: ")
        combo_layout = QHBoxLayout()
        chk_button = QPushButton(self)
        chk_button.setText("Check")
        chk_button.clicked.connect(self.validate_entered_uri)
        self.combo.lineEdit().returnPressed.connect(chk_button.click)
        combo_layout.addWidget(combo_label)
        combo_layout.addWidget(self.combo)
        combo_layout.addWidget(chk_button)

        main_layout.addLayout(combo_layout)

        result_label = QLabel(self)
        result_label.setText("Metadata found at the given address: ")
        self.result_text_box = QTextBrowser(self)
        result_layout = QVBoxLayout()
        result_layout.addWidget(result_label)
        result_layout.addWidget(self.result_text_box)

        main_layout.addLayout(result_layout)

        self.qbuttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.qbuttons.accepted.connect(self.accept)
        self.qbuttons.rejected.connect(self.reject)
        self.qbuttons.button(QDialogButtonBox.Ok).setText("Add to project")
        self.qbuttons.button(QDialogButtonBox.Ok).setEnabled(False)

        def update_ok_button(current_entered_text):
            if current_entered_text == self.selected_url:
                self.qbuttons.button(QDialogButtonBox.Ok).setEnabled(True)
            else:
                self.qbuttons.button(QDialogButtonBox.Ok).setEnabled(False)

        self.combo.lineEdit().textChanged.connect(update_ok_button)
        main_layout.addWidget(self.qbuttons)
        self.setLayout(main_layout)

    def validate_entered_uri(self, _event):
        self.selected_url = None
        url = self.combo.currentText().strip()
        if url == "":
            return
        if not isUrl(url):
            self._set_text_input_to_guessed_uri(url)
            return
        logger.debug(f"Entered URL: {url}")
        try:
            # Ask each store type if it likes the URL to avoid web requests during instantiation attempts.
            if OMEZarrStore.is_url_compatible(url):
                rv = OMEZarrStore(url)
            elif RESTfulPrecomputedChunkedVolume.is_url_compatible(url):
                rv = RESTfulPrecomputedChunkedVolume(volume_url=url)
            else:
                store_types = [OMEZarrStore, RESTfulPrecomputedChunkedVolume]
                supported_formats = "\n".join([f"{s.NAME} ({s.URL_HINT})" for s in store_types])
                msg = f"Address does not look like any supported format.\n\nSupported formats:\n{supported_formats}"
                self.result_text_box.setText(msg)
                return
        except Exception as e:
            self.qbuttons.button(QDialogButtonBox.Ok).setEnabled(False)
            if isinstance(e, SSLError):
                msg = "SSL error, please check that you are using the correct protocol (http/https)."
            elif isinstance(e, ConnectionError):
                msg = "Connection error, please check that the server is online and the URL is correct."
            else:
                msg = "Unknown error while trying to connect to this address."
            msg += f"\n\nFull error message:\n{e}"
            self.result_text_box.setText(msg)
            return

        self.selected_url = url
        self.result_text_box.setText(
            f"URL: {self.selected_url}\n"
            f"Data format: {rv.NAME}\n"
            f"Number of scales: {len(rv.multiscales)}\n"
            f"Raw dataset shape: {rv.get_shape(rv.highest_resolution_key)}\n"
            f"Lowest scale shape: {rv.get_shape(rv.lowest_resolution_key)}\n"
        )
        # This check-button might have been triggered by pressing Enter.
        # The timer prevents triggering the now enabled OK button by the same keypress.
        QTimer.singleShot(0, lambda: self.qbuttons.button(QDialogButtonBox.Ok).setEnabled(True))

    def _set_text_input_to_guessed_uri(self, path):
        ospath = pathlib.Path(path)
        if ospath.exists():
            guessed_uri = ospath.as_uri()
        else:
            guessed_uri = f"https://{path}"
        self.combo.lineEdit().setText(guessed_uri)
        msg = (
            'Address must be a URI starting with "http(s)://" or "file://".\n\n'
            "Your address was modified as a guess, please try again if it looks good."
        )
        self.result_text_box.setText(msg)


if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication

    app = QApplication([])

    logging.basicConfig(level=logging.INFO)

    pv = MultiscaleDatasetBrowser()
    pv.combo.addItem("test")
    pv.show()
    app.exec_()
    print(pv.result(), pv.selected_url)
