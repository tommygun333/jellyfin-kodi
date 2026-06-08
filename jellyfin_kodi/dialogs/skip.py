# -*- coding: utf-8 -*-
from __future__ import division, absolute_import, print_function, unicode_literals

import xbmcgui

from ..helper import LazyLogger

LOG = LazyLogger(__name__)

# Action IDs
ACTION_PARENT_DIR = 9
ACTION_PREVIOUS_MENU = 10
ACTION_BACK = 92

# Control IDs
SKIP_BUTTON = 3012

# Segment type -> short display label
SEGMENT_LABELS = {
    "Introduction": "Intro",
    "Credits": "Credits",
    "Recap": "Recap",
    "Preview": "Preview",
    "Commercial": "Ad",
}


class SkipDialog(xbmcgui.WindowXMLDialog):
    """
    OSD overlay dialog for skipping intro/outro segments.
    Single rounded white button — same design as slyguy.skip_intro.
    """

    def __init__(self, *args, **kwargs):
        self._segment_type = kwargs.pop("segment_type", None)
        self.skip_requested = False
        xbmcgui.WindowXMLDialog.__init__(self, *args)

    def set_skip_info(self, segment_type):
        """Set button label from segment type.  No duration text appended."""
        self._segment_type = segment_type
        segment_label = SEGMENT_LABELS.get(segment_type, segment_type or "Segment")
        button_label = u">  Skip {0}".format(segment_label)
        self.setProperty("skip_label", button_label)
        LOG.debug("SkipDialog.set_skip_info: label=%s", button_label)

    def onInit(self):
        self.setProperty("skip_label", self.getProperty("skip_label"))
        try:
            self.setFocus(self.getControl(SKIP_BUTTON))
        except Exception as exc:
            LOG.debug("SkipDialog.onInit setFocus failed: %s", exc)

    def onAction(self, action):
        if action.getId() in (ACTION_BACK, ACTION_PARENT_DIR, ACTION_PREVIOUS_MENU):
            self.close()

    def onClick(self, control_id):
        if control_id == SKIP_BUTTON:
            self.skip_requested = True
            self.close()

    def is_skip(self):
        return self.skip_requested
