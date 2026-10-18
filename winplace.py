"""Window-placement arithmetic, with no tkinter and no display: parse the
X11 work area from xprop output, and fit a window of a wanted size inside a
work area. geometry_mixin does the Tk side effects; this is the maths it
uses, kept apart so it is unit-tested headless."""

import re


def parse_net_workarea(xprop_output):
    """Parse the first desktop's (x, y, w, h) from an ``xprop -root _NET_WORKAREA``
    output string.

    A typical line looks like::

        _NET_WORKAREA(CARDINAL) = 0, 0, 1920, 1053, 0, 0, 1920, 1053

    The first four integers after the ``=`` are x, y, w, h for desktop 0.
    Returns a ``(x, y, w, h)`` tuple of ints, or ``None`` when the string is
    empty, contains the "not found" sentinel, or has fewer than four numbers.
    """
    if not xprop_output or not xprop_output.strip():
        return None
    # xprop writes "_NET_WORKAREA:  not found." when the property is absent.
    if "not found" in xprop_output.lower():
        return None
    # Take everything after the first "=" so we skip the property-name header.
    after_eq = xprop_output.split("=", 1)[-1]
    # Parse signs honestly so a negative origin is reported as-is and the caller
    # (which sanity-checks for non-negative coords) can reject it, rather than a
    # stripped minus turning it into a wrong-but-positive value.
    nums = [int(n) for n in re.findall(r"-?\d+", after_eq)]
    if len(nums) < 4:
        return None
    return (nums[0], nums[1], nums[2], nums[3])

def compute_window_fit(wa_x, wa_y, wa_w, wa_h, desired_w, desired_h, reserve=80):
    """Return ``(w, h, x, y)`` so a window of *desired* size fits inside the
    given work area without overlapping a taskbar or panel.

    Parameters
    ----------
    wa_x, wa_y : int
        Top-left corner of the work area (may be non-zero if a panel sits on
        the left or top).
    wa_w, wa_h : int
        Width and height of the work area.
    desired_w, desired_h : int
        Preferred window dimensions.  The window only shrinks below these when
        the work area is too small.
    reserve : int
        Vertical pixels reserved for the window-manager title bar so that the
        bottom edge of the client area never lands under the taskbar on small
        screens.  Defaults to 80.

    Returns
    -------
    (final_w, final_h, x, y) : tuple[int, int, int, int]
        The size and top-left position to pass to ``window.geometry()``.
        Every edge is guaranteed to lie within the work area.
    """
    avail_h = max(wa_h - reserve, 200)
    # Cap to the actual work area as well, so even on a degenerate screen smaller
    # than the 200px reserve floor the window can never exceed (and overflow) the
    # work area — keeping the "every edge inside the work area" guarantee.
    final_w = max(1, min(desired_w, wa_w))
    final_h = max(1, min(desired_h, avail_h, wa_h))
    # Centre horizontally; vertically leave reserve/2 of slack above and below
    # so neither the title bar nor the bottom buttons land under a panel/taskbar.
    x = wa_x + (wa_w - final_w) // 2
    y = wa_y + (reserve // 2) + (avail_h - final_h) // 2
    # Clamp every edge inside the work area.
    x = max(wa_x, min(x, wa_x + wa_w - final_w))
    y = max(wa_y, min(y, wa_y + wa_h - final_h))
    return final_w, final_h, x, y
