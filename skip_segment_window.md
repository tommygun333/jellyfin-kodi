# Skip Segment Overlay Window in Kodi (jellyfin-kodi)

This document explains how the skip segment overlay window works in the jellyfin-kodi addon, covering the full pipeline from fetching segment data to showing and monitoring the dialog. It is intended as a reference for developers building similar overlay windows in other Kodi addons.

---

## Overview

When a Jellyfin media item is playing, the addon periodically checks whether playback has entered a known media segment (e.g., an intro, credits, or commercial). Depending on user settings, it can:

1. **Auto-skip** the segment instantly.
2. **Show an OSD button** letting the user decide to skip or dismiss.
3. **Trigger "Play Next"** to move to the next episode.

The overlay window described here is the "Show skip button" (mode 2) variant — a non-blocking, non-modal dialog that slides in from the bottom right corner while video plays uninterrupted in the background.

---

## Architecture: Key Files

| File | Purpose |
|---|---|
| `jellyfin_kodi/player.py` | Main `Player` class; fetches segments, monitors position, triggers the dialog |
| `jellyfin_kodi/dialogs/skip.py` | `SkipDialog` class; the Python side of the overlay window |
| `resources/skins/default/1080i/script-jellyfin-skip.xml` | XML skin layout for the overlay window |
| `jellyfin_kodi/jellyfin/api.py` | Jellyfin API call to retrieve media segments |
| `resources/settings.xml` | User-configurable skip modes for each segment type |

---

## Step 1 — Fetching Media Segments from the Server

When playback starts (`onPlayBackStarted`), the player calls `_fetch_skip_segments()`, which queries the Jellyfin API for the `MediaSegments` of the current item. This is available from Jellyfin server 10.10+.

```python
# jellyfin_kodi/jellyfin/api.py

def get_media_segments(self, item_id):
    """Get media segments for an item (Jellyfin 10.10+)."""
    try:
        return self._get("MediaSegments/%s" % item_id)
    except HTTPException as e:
        if e.status == 404:
            LOG.debug("Media Segments not available for %s", item_id)
        else:
            LOG.warning("Error fetching media segments: %s", e)
        return None
    except Exception as e:
        LOG.warning("Error fetching media segments: %s", e)
        return None
```

The raw API response (a list of segment objects in `Items`) is then converted into a normalised internal dictionary by `_convert_media_segments()`:

```python
# jellyfin_kodi/player.py

def _convert_media_segments(self, response):
    if not response or "Items" not in response:
        return None

    type_map = {
        "Intro": "Introduction",
        "Outro": "Credits",
        "Recap": "Recap",
        "Preview": "Preview",
        "Commercial": "Commercial",
    }

    segments = {}
    for item in response["Items"]:
        seg_type = type_map.get(item.get("Type"))
        if seg_type:
            segments[seg_type] = {
                "EpisodeId": item.get("ItemId"),
                "Start": item.get("StartTicks", 0) / 10000000.0,  # ticks → seconds
                "End":   item.get("EndTicks",   0) / 10000000.0,
            }
    return segments if segments else None
```

> **Important:** Jellyfin stores timestamps as *ticks* (100-nanosecond units). Dividing by `10 000 000` converts them to seconds.

The resulting dictionary is stored in the class-level `skip_segments` dict keyed by item ID:

```python
# class-level state in Player
skip_segments = {}       # {item_id: {"Introduction": {"Start": ..., "End": ...}, ...}}
skip_prompted = set()    # tracks which segments have already been handled this playback
skip_dialog   = None     # current SkipDialog instance, or None
```

---

## Step 2 — Checking Position During Playback

The position check is triggered from three places:

1. **`onPlayBackStarted`** — immediately after segments load (catches segments that start at 0 s).
2. **`report_playback`** — called on a regular heartbeat during playback (Kodi's progress reporting loop).
3. **`onPlayBackSeek`** — right after the user seeks, so the overlay pops up if they seek into a segment.

All three call `check_skip_segments(item, current_position)`:

```python
def check_skip_segments(self, item, current_position):
    item_id  = item["Id"]
    segments = self.skip_segments.get(item_id)
    if not segments:
        return

    for segment_type, segment in segments.items():
        skip_mode = self._get_segment_skip_mode(segment_type)
        if skip_mode == 0:   # disabled in settings
            continue

        bounds = self._process_segment(
            item_id, segment_type, segment, current_position, skip_mode
        )
        if not bounds:       # not inside this segment
            continue

        start, end = bounds
        segment_key = "%s:%s" % (item_id, segment_type)

        if segment_key in self.skip_prompted:  # already handled
            continue

        self.skip_prompted.add(segment_key)   # mark as handled

        self._handle_skip_segment(segment_type, start, end, skip_mode)
        break  # handle one segment per check
```

The `skip_prompted` set prevents the same segment from triggering the dialog multiple times within the same playback session — even if the user manually seeks back into the segment.

`_process_segment()` simply verifies the position is within bounds and returns the `(start, end)` tuple (or `None` if outside):

```python
def _process_segment(self, item_id, segment_type, segment, current_position, skip_mode):
    start = segment.get("Start")
    end   = segment.get("End")
    if start is None or end is None or end <= start:
        return False

    if not (start <= current_position <= end):
        return None

    return (start, end)
```

---

## Step 3 — Determining Skip Mode Per Segment

Each segment type has its own user-configurable skip mode in the addon settings:

| Setting ID | Segment Type | Allowed Modes |
|---|---|---|
| `skipIntroductionMode` | Introduction (Intro) | Off / Auto / Button |
| `skipCreditsMode` | Credits (Outro) | Off / Auto / Button / Play Next |
| `skipRecapMode` | Recap | Off / Auto / Button |
| `skipPreviewMode` | Preview | Off / Auto / Button / Play Next |
| `skipCommercialMode` | Commercial | Off / Auto / Button |

```python
def _get_segment_skip_mode(self, segment_type):
    setting_map = {
        "Introduction": "skipIntroductionMode",
        "Credits":      "skipCreditsMode",
        "Recap":        "skipRecapMode",
        "Preview":      "skipPreviewMode",
        "Commercial":   "skipCommercialMode",
    }
    setting_key = setting_map.get(segment_type)
    if not setting_key:
        return 0
    return int(settings(setting_key) or 0)
```

Mode values:

* `0` — Off (do nothing)
* `1` — Auto-skip (seek to end immediately + notification)
* `2` — Show skip button (overlay window)
* `3` — Play Next (for Credits / Preview only)

`_handle_skip_segment()` dispatches to the correct behaviour:

```python
def _handle_skip_segment(self, segment_type, start, end, mode):
    if mode == 1:   # Auto skip
        self.seekTime(end)
        dialog("notification", heading="Jellyfin",
               message="Skipped %s" % segment_type,
               icon="{jellyfin}", time=3000)

    elif mode == 2: # Show skip button (overlay window)
        self._show_skip_button(segment_type, end - start, end)

    elif mode == 3: # Play Next
        self._handle_play_next(segment_type)
```

---

## Step 4 — Creating the Overlay Window

`_show_skip_button()` is where the overlay window is instantiated:

```python
def _show_skip_button(self, segment_type, duration, end_time):
    import xbmcaddon
    from .dialogs.skip import SkipDialog

    # Close any previous dialog first
    if self.skip_dialog:
        try:
            self.skip_dialog.close()
        except Exception:
            pass

    # Resolve path to this addon's skin resources
    addon_path = xbmcaddon.Addon("plugin.video.jellyfin").getAddonInfo("path")

    # Instantiate the WindowXMLDialog subclass, pointing to the XML skin file
    self.skip_dialog = SkipDialog(
        "script-jellyfin-skip.xml",  # skin XML filename
        addon_path,                  # path where the XML lives
        "default",                   # skin name
        "1080i",                     # skin resolution folder
    )

    # Set dynamic properties BEFORE show() so the skin can read them
    self.skip_dialog.set_skip_info(segment_type, duration)

    self.skip_dialog.show()          # non-blocking; video keeps playing

    self._skip_end_time = end_time
    self._monitor_skip_dialog()      # enter polling loop
```

### Why `WindowXMLDialog` and not `WindowXML`?

`xbmcgui.WindowXMLDialog` is a *dialog* window: it renders on top of the current window (the fullscreen video player) without replacing it. The video layer remains fully active underneath. `WindowXML` would push a new window onto the stack and obscure the player.

### Why `show()` instead of `doModal()`?

`doModal()` blocks the calling thread until the dialog is closed. That would freeze playback reporting and the entire player event loop. `show()` displays the dialog non-modally and returns immediately, so the addon can continue monitoring playback in a loop.

---

## Step 5 — The SkipDialog Class

```python
# jellyfin_kodi/dialogs/skip.py

import xbmcgui

ACTION_PARENT_DIR   = 9
ACTION_PREVIOUS_MENU = 10
ACTION_BACK         = 92
ACTION_NAV_BACK     = 92

SKIP_BUTTON  = 3012  # must match id in XML
CLOSE_BUTTON = 3013  # must match id in XML

SEGMENT_LABELS = {
    "Introduction": "Intro",
    "Credits":      "Outro",
    "Recap":        "Recap",
    "Preview":      "Preview",
    "Commercial":   "Ad",
}

class SkipDialog(xbmcgui.WindowXMLDialog):

    def __init__(self, *args, **kwargs):
        self._segment_type = kwargs.pop("segment_type", None)
        self._duration     = kwargs.pop("duration", 0)
        self.skip_requested   = False
        self.cancel_requested = False
        xbmcgui.WindowXMLDialog.__init__(self, *args)

    def set_skip_info(self, segment_type, duration):
        """Store data and push skin properties before the window opens."""
        self._segment_type = segment_type
        self._duration     = duration

        minutes = int(duration // 60)
        seconds = int(duration % 60)
        duration_text = "{0}m {1}s".format(minutes, seconds) if minutes > 0 \
                        else "{0}s".format(seconds)

        segment_label = SEGMENT_LABELS.get(segment_type, segment_type or "Segment")
        button_label  = "Skip {0} ({1})".format(segment_label, duration_text)

        # setProperty stores values that the XML skin reads via $INFO[Window.Property(...)]
        self.setProperty("skip_label", button_label)
        self.setProperty("segment_type", segment_type or "")
        self.setProperty("duration", duration_text)

    def onInit(self):
        """Called by Kodi after the window's XML has been parsed and controls created."""
        try:
            button = self.getControl(SKIP_BUTTON)
            label  = self.getProperty("skip_label")
            if label:
                button.setLabel(label)   # belt-and-braces: also set label directly
        except Exception as e:
            LOG.debug("Could not set skip button label: %s", e)

    def onAction(self, action):
        """Handle remote/keyboard back navigation."""
        if action.getId() in (ACTION_BACK, ACTION_PARENT_DIR,
                               ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self.cancel_requested = True
            self.close()

    def onClick(self, control_id):
        """Handle button clicks."""
        if control_id == SKIP_BUTTON:
            self.skip_requested = True
            self.close()
        elif control_id == CLOSE_BUTTON:
            self.cancel_requested = True
            self.close()

    def is_skip(self):
        return self.skip_requested

    def is_cancel(self):
        return self.cancel_requested
```

### Key Points

- **`set_skip_info()` is called before `show()`** so that `setProperty()` values are ready when `onInit()` fires. If you call it after `show()`, there is a race condition where the skin renders before data is available.
- **`onInit()`** is the earliest point where `getControl()` is safe. The XML controls are not yet created when `__init__` runs.
- **State flags** (`skip_requested`, `cancel_requested`) are simple booleans. The monitoring loop in `player.py` polls them — no threading or event callbacks are needed.
- **`close()`** hides the dialog but does not destroy it. The monitor loop detects the state change and then sets `self.skip_dialog = None`.

---

## Step 6 — The Monitoring Loop

Because `show()` is non-blocking, a polling loop runs after it to watch for user interaction or end-of-segment:

```python
def _monitor_skip_dialog(self):
    monitor = xbmc.Monitor()

    while self.skip_dialog and not monitor.abortRequested():

        if self.skip_dialog.is_skip():
            self.seekTime(self._skip_end_time)   # user clicked Skip
            break

        if self.skip_dialog.is_cancel():
            break                                # user dismissed

        try:
            current_pos = self.getTime()
            if current_pos >= self._skip_end_time:
                break                            # segment ended naturally
        except Exception:
            break

        if monitor.waitForAbort(0.2):            # 200 ms poll interval
            break

    # Cleanup
    if self.skip_dialog:
        try:
            self.skip_dialog.close()
        except Exception:
            pass
        self.skip_dialog = None
```

`monitor.waitForAbort(0.2)` serves two purposes:
1. It introduces a short sleep to avoid busy-waiting.
2. It returns `True` immediately if Kodi is shutting down, allowing clean exit.

---

## Step 7 — The XML Skin Layout

The window layout is defined in:

```
resources/skins/default/1080i/script-jellyfin-skip.xml
```

Key structural decisions:

```xml
<window>
  <!-- Auto-focus the skip button when the window opens -->
  <defaultcontrol always="true">3012</defaultcontrol>

  <!-- Dismiss any fullscreen OSD info bar that might overlap -->
  <onload>Dialog.Close(fullscreeninfo,true)</onload>

  <controls>
    <control type="group">

      <!-- Slide-in animation from the right -->
      <animation type="WindowOpen" reversible="false">
        <effect type="fade"  start="0"   end="100" time="300" />
        <effect type="slide" start="400,0" end="0,0" time="300" tween="cubic" easing="out"/>
      </animation>
      <animation type="WindowClose" reversible="false">
        <effect type="fade"  start="100" end="0"   time="300" />
        <effect type="slide" start="0,0" end="400,0" time="300" tween="cubic" easing="in"/>
      </animation>

      <!-- Bottom-right position (for a 1920×1080 canvas) -->
      <left>1420</left>
      <top>920</top>

      <control type="group">
        <width>460</width>
        <height>80</height>

        <!-- Semi-transparent dark background -->
        <control type="image">
          <texture colordiffuse="E6000000">white.png</texture>
          <aspectratio>stretch</aspectratio>
        </control>

        <!-- Jellyfin blue accent bar -->
        <control type="image">
          <width>4</width>
          <texture colordiffuse="FF00A4DC">white.png</texture>
        </control>

        <!-- Buttons side by side -->
        <control type="grouplist">
          <orientation>horizontal</orientation>

          <!-- Skip button: label comes from a skin property set by Python -->
          <control type="button" id="3012">
            <label>$INFO[Window.Property(skip_label)]</label>
            <texturefocus   colordiffuse="FF00A4DC">white.png</texturefocus>
            <texturenofocus colordiffuse="33FFFFFF">white.png</texturenofocus>
          </control>

          <!-- Close / dismiss button -->
          <control type="button" id="3013">
            <label>Close</label>
            ...
          </control>
        </control>
      </control>
    </control>
  </controls>
</window>
```

### Critical XML Details

| Detail | Explanation |
|---|---|
| No `id` or `type` attribute on `<window>` | Makes this a plain overlay window (not `dialog`-typed). The Python class (`WindowXMLDialog`) already handles the dialog semantics. |
| `<defaultcontrol always="true">3012</defaultcontrol>` | The skip button is focused automatically on every render, including after gaining focus from another control. This means remote users do not need to navigate to it. |
| `<onload>Dialog.Close(fullscreeninfo,true)</onload>` | Closes Kodi's own fullscreen info overlay (which shows title, progress bar, etc.) if it is open. Without this the info bar and the skip button can visually conflict. |
| Skin property read: `$INFO[Window.Property(skip_label)]` | Reads the value set by `self.setProperty("skip_label", ...)` in Python. This is the standard mechanism for passing dynamic text to a `WindowXMLDialog`. |
| `white.png` as texture | A solid-white 1×1 pixel image (standard Kodi convention). `colordiffuse` tints it to any ARGB colour. This avoids needing separate texture assets for backgrounds and accent bars. |

---

## Settings Integration

Media segment support is gated by a master toggle and each segment type has its own mode spinner:

```xml
<!-- resources/settings.xml -->
<setting id="mediaSegmentsEnabled" type="boolean" label="33247">
    <default>true</default>
    <control type="toggle"/>
</setting>

<setting id="skipIntroductionMode" type="integer" label="33252"
         parent="mediaSegmentsEnabled">
    <default>2</default>  <!-- 2 = Show button -->
    <constraints>
        <options>
            <option label="33261">0</option>  <!-- Off -->
            <option label="33249">1</option>  <!-- Auto skip -->
            <option label="33250">2</option>  <!-- Show button -->
        </options>
    </constraints>
</setting>
```

The `parent="mediaSegmentsEnabled"` attribute on child settings makes them automatically invisible when the master toggle is off.

In Python, settings are read using the `settings()` helper:

```python
if not settings("mediaSegmentsEnabled.bool"):
    return   # feature disabled, skip everything

mode = int(settings("skipIntroductionMode") or 0)
```

---

## Full Sequence Diagram

```
onPlayBackStarted()
    └─► _fetch_skip_segments(item)
            └─► api.get_media_segments(item_id)  → {Items: [...]}
            └─► _convert_media_segments()         → {"Introduction": {Start, End}, ...}
            └─► skip_segments[item_id] = segments

report_playback() / onPlayBackSeek()
    └─► check_skip_segments(item, current_pos)
            └─► for each segment_type:
                    └─► _get_segment_skip_mode()  → mode (0/1/2/3)
                    └─► _process_segment()        → (start, end) or None
                    └─► if not in skip_prompted:
                            └─► skip_prompted.add(key)
                            └─► _handle_skip_segment(type, start, end, mode)

_handle_skip_segment(mode=2)
    └─► _show_skip_button(segment_type, duration, end_time)
            └─► SkipDialog("script-jellyfin-skip.xml", addon_path, "default", "1080i")
            └─► skip_dialog.set_skip_info(segment_type, duration)
            └─► skip_dialog.show()       ← non-blocking, video keeps playing
            └─► _monitor_skip_dialog()   ← polling loop (200 ms)
                    ├─► is_skip()   → seekTime(end_time) + break
                    ├─► is_cancel() → break
                    └─► getTime() >= end_time → break
            └─► skip_dialog.close()
            └─► skip_dialog = None
```

---

## Building a Similar Overlay Window in Another Addon

The pattern is generic and reusable. Here is the minimal recipe:

### 1. Create the XML skin file

Place it in:
```
resources/skins/default/1080i/my-addon-overlay.xml
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<window>
    <defaultcontrol always="true">9001</defaultcontrol>
    <controls>
        <control type="group">
            <animation type="WindowOpen" reversible="false">
                <effect type="fade" start="0" end="100" time="300"/>
            </animation>
            <animation type="WindowClose" reversible="false">
                <effect type="fade" start="100" end="0" time="300"/>
            </animation>
            <!-- Position wherever you want -->
            <left>100</left>
            <top>900</top>
            <control type="button" id="9001">
                <label>$INFO[Window.Property(my_label)]</label>
                <width>300</width>
                <height>60</height>
                <texturefocus   colordiffuse="FF00A4DC">white.png</texturefocus>
                <texturenofocus colordiffuse="AA000000">white.png</texturenofocus>
            </control>
        </control>
    </controls>
</window>
```

### 2. Create the Python dialog class

```python
import xbmcgui

MY_BUTTON = 9001
ACTION_BACK = 92

class MyOverlay(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self.confirmed = False
        self.cancelled = False
        xbmcgui.WindowXMLDialog.__init__(self, *args)

    def set_label(self, text):
        # Call BEFORE show() so onInit sees the value
        self.setProperty("my_label", text)

    def onInit(self):
        # getControl() is safe here; controls are fully built
        try:
            self.getControl(MY_BUTTON).setLabel(self.getProperty("my_label"))
        except Exception:
            pass

    def onAction(self, action):
        if action.getId() == ACTION_BACK:
            self.cancelled = True
            self.close()

    def onClick(self, control_id):
        if control_id == MY_BUTTON:
            self.confirmed = True
            self.close()
```

### 3. Show and monitor the overlay

```python
import xbmc
import xbmcaddon

addon_path = xbmcaddon.Addon("plugin.video.myaddon").getAddonInfo("path")

overlay = MyOverlay(
    "my-addon-overlay.xml",   # filename
    addon_path,               # path to addon root
    "default",                # skin name
    "1080i",                  # resolution folder
)
overlay.set_label("Click Me")
overlay.show()   # non-blocking

monitor = xbmc.Monitor()
while not monitor.abortRequested():
    if overlay.confirmed:
        # handle confirmation
        break
    if overlay.cancelled:
        break
    if monitor.waitForAbort(0.2):
        break

overlay.close()
```

### Common Mistakes

| Mistake | Effect | Fix |
|---|---|---|
| Using `doModal()` instead of `show()` | Blocks the thread; playback reporting freezes | Always use `show()` for non-blocking overlays during playback |
| Calling `setProperty()` after `show()` | Race condition; skin may render before data is set | Call `set_*` methods before `show()` |
| Calling `getControl()` in `__init__` | `RuntimeError`; controls don't exist yet | Only call `getControl()` in `onInit()` or later |
| Using `WindowXML` instead of `WindowXMLDialog` | Pushes a new window, obscuring the video player | Use `WindowXMLDialog` for overlays on top of the player |
| Forgetting `<defaultcontrol>` | Dialog opens with no focused element; remote navigation broken | Always set a sensible default focused control |
| Not calling `close()` on cleanup | Dialog stays on screen after its purpose is done | Always close the dialog in the cleanup path |
| Reusing the same dialog instance after `close()` | Undefined behaviour | Create a new instance each time |

---

## Summary

The skip segment overlay in jellyfin-kodi works by:

1. Fetching timestamped segment data from the Jellyfin `MediaSegments` API on playback start.
2. Converting tick-based timestamps to seconds and storing them per item.
3. Polling the current playback position during the normal reporting heartbeat.
4. When position enters a segment (and it hasn't been handled before), checking the user's chosen mode.
5. For "Show button" mode: instantiating a `WindowXMLDialog` subclass, setting skin properties, calling `show()`, then polling in a loop until the user responds or the segment ends.
6. Using a plain XML skin file in the addon's own `resources/skins` tree to define the visual layout, with positions and colours baked in for a 1920×1080 canvas.
