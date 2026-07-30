# SPDX-License-Identifier: GPL-3.0-or-later
#
# Grab N Tab - drag-and-drop ordering, hiding and pinning of 3D Viewport
# sidebar (N-panel) tabs, with automatic persistence across sessions/files.
#
# How it works (the parts that are deliberately different from other tab
# managers):
#
#   * Tab order in Blender == panel registration order.  The addon keeps a
#     saved category order and *re-registers* sidebar panels (dependency-aware:
#     children follow parents) so the strip matches it.  No category renaming,
#     no manual per-addon mapping.
#   * The *true* current tab order is read back from Blender itself via the
#     runtime enum on `Region.active_panel_category` (assigning an invalid
#     value raises an error that lists every category in display order).
#     That list is regenerated when the region draws, so we re-sync from it
#     every time the user interacts with the strip - the addon can never go
#     stale, even if something else reorders panels behind our back.
#   * `bpy.utils.register_class` / `unregister_class` are wrapped while the
#     addon is enabled.  When any other addon (re)registers sidebar panels -
#     enabling, disabling, updating, deferred registration - a debounced timer
#     re-applies your layout automatically.  Hidden tabs stay hidden, order
#     stays yours, with zero manual refreshing.
#   * Hidden tabs are unregistered but cached; if their owner addon is
#     disabled while hidden, our unregister wrapper answers for the cached
#     classes so the owner addon's own cleanup never sees an error.
#   * State is stored as JSON in Blender's per-user config directory and is
#     written atomically on every change - it does not depend on
#     "Save Preferences" and survives new files, restarts and crashes.

bl_info = {
    "name": "Grab N Tab",
    "author": "mrtlsw",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport Sidebar (N)  -  Ctrl+Drag/G moves a tab, H hides, M menu, '≡' tab organizes",
    "description": "Drag-and-drop reordering, hiding and pinning of N-panel tabs. Order persists automatically.",
    "category": "Interface",
}

import json
import math
import os
import re
import time
import traceback

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

SPACE = 'VIEW_3D'
ORGANIZER_CAT = "≡"          # the addon's own slim organizer tab
LOG_PREFIX = "[Grab N Tab] "
LIVE_APPLY_BUDGET_MS = 60.0       # live-reorder while dragging if apply is faster than this

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_APPLYING = False                 # reconciler re-entrancy guard
_SHUTDOWN = False                 # set during unregister()
_state = None                     # loaded JSON state (dict)
_current_applied = None           # visible category order we believe is live
_hidden_cache = {}                # category -> [panel classes] (unregistered)
_apply_ms = None                  # duration of the last full apply, in ms
_schedule_pending = False
_ctx_menu_target = ""             # category targeted by the right-click menu
_keymap_items = []                # [(KeyMap, KeyMapItem)]
_seed_tries = 0


def _log(*args):
    print(LOG_PREFIX + " ".join(str(a) for a in args))


# ---------------------------------------------------------------------------
# Persistent state (JSON in the user config dir - atomic writes)
# ---------------------------------------------------------------------------

def _config_path():
    d = bpy.utils.user_resource('CONFIG', path="grab_n_tab", create=True)
    return os.path.join(d, "state.json")


def _default_state():
    return {"version": 1,
            "spaces": {SPACE: {"order": [], "hidden": [], "pinned": []}}}


def load_state():
    global _state
    _state = _default_state()
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "spaces" in data:
            sp = data["spaces"].get(SPACE, {})
            dst = _state["spaces"][SPACE]
            for key in ("order", "hidden", "pinned"):
                val = sp.get(key, [])
                if isinstance(val, list):
                    dst[key] = [str(v) for v in val if isinstance(v, str)]
    except FileNotFoundError:
        pass
    except Exception:
        _log("could not read saved state, starting fresh:")
        traceback.print_exc()


def save_state():
    if _state is None:
        return
    try:
        path = _config_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        _log("could not save state:")
        traceback.print_exc()


def space_state():
    if _state is None:
        load_state()
    return _state["spaces"][SPACE]


# ---------------------------------------------------------------------------
# Panel introspection
# ---------------------------------------------------------------------------

def _walk_panel_classes():
    """Every Panel subclass, in class-creation order (breadth-first)."""
    out, seen, queue = [], set(), [bpy.types.Panel]
    while queue:
        cls = queue.pop(0)
        try:
            subs = cls.__subclasses__()
        except Exception:
            continue
        for sub in subs:
            if sub in seen:
                continue
            seen.add(sub)
            out.append(sub)
            queue.append(sub)
    return out


def _idname(cls):
    return getattr(cls, 'bl_idname', cls.__name__)


def build_snapshot():
    """roots_by_cat: {category: [root classes]}, children: {parent idname: [classes]}.

    Only fully-registered VIEW_3D sidebar panels are 'managed'.  Child panels
    (bl_parent_id) are grouped under the root of their parent chain regardless
    of their own declared category.
    """
    registered = [c for c in _walk_panel_classes()
                  if getattr(c, 'is_registered', False)]
    by_id = {}
    for c in registered:
        by_id.setdefault(_idname(c), c)

    roots_by_cat, managed_roots = {}, set()
    for c in registered:
        if getattr(c, 'bl_parent_id', ''):
            continue
        if getattr(c, 'bl_region_type', None) != 'UI':
            continue
        if getattr(c, 'bl_space_type', None) != SPACE:
            continue
        cat = getattr(c, 'bl_category', '') or "Misc"
        if cat == ORGANIZER_CAT:
            continue
        roots_by_cat.setdefault(cat, []).append(c)
        managed_roots.add(c)

    children = {}
    for c in registered:
        pid = getattr(c, 'bl_parent_id', '')
        if not pid:
            continue
        # climb to the root of the parent chain
        node, hops, seen = c, 0, set()
        while getattr(node, 'bl_parent_id', '') and hops < 32:
            if node in seen:
                node = None
                break
            seen.add(node)
            node = by_id.get(node.bl_parent_id)
            if node is None:
                break
            hops += 1
        if node in managed_roots:
            children.setdefault(pid, []).append(c)
    return roots_by_cat, children


def _tree_flat(root, children):
    """Root followed by all descendants, parents always before children."""
    out = [root]
    for ch in children.get(_idname(root), ()):
        out.extend(_tree_flat(ch, children))
    return out


def _cat_flat(cat, roots_by_cat, children):
    out = []
    for root in roots_by_cat.get(cat, ()):
        out.extend(_tree_flat(root, children))
    return out


# ---------------------------------------------------------------------------
# Reading the *actual* tab order back from Blender
# ---------------------------------------------------------------------------

def _find_sidebar_region():
    wm = bpy.context.window_manager
    if wm is None:
        return None
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == SPACE:
                for r in area.regions:
                    if r.type == 'UI':
                        return r
    return None


def read_display_order(region=None):
    """Exact current tab order, straight from Blender's runtime category enum.

    Assigning an invalid value to region.active_panel_category raises an error
    whose message lists every valid category *in display order*.  The list is
    rebuilt whenever the region draws, so treat the result as
    'order as of the last draw'.  Returns [] when unavailable.
    """
    region = region or _find_sidebar_region()
    if region is None:
        return []
    try:
        region.active_panel_category = "__GNT_PROBE__"
    except Exception as exc:
        m = re.search(r"not found in \((.*)\)", str(exc))
        if m:
            cats = re.findall(r"'([^']*)'", m.group(1))
            return [c for c in cats if c and c != ORGANIZER_CAT]
    return []


# ---------------------------------------------------------------------------
# The reconciler
# ---------------------------------------------------------------------------

def _orig_register():
    fn = bpy.utils.register_class
    return getattr(fn, '_gnt_orig', fn)


def _orig_unregister():
    fn = bpy.utils.unregister_class
    return getattr(fn, '_gnt_orig', fn)


def _safe_unreg(cls):
    try:
        if getattr(cls, 'is_registered', False):
            _orig_unregister()(cls)
    except Exception:
        _log("failed to unregister", _idname(cls))
        traceback.print_exc()


def _safe_reg(cls, errors):
    try:
        if not getattr(cls, 'is_registered', False):
            _orig_register()(cls)
    except Exception as exc:
        errors.append((_idname(cls), repr(exc)))
        _log("failed to re-register", _idname(cls), repr(exc))


def _move_cat_to_end(flat, errors):
    """Re-register a category's panel trees, moving its tab to the end of the
    strip.  Children are unregistered first and registered after their parents,
    so parent links never dangle."""
    for cls in reversed(flat):
        _safe_unreg(cls)
    for cls in flat:
        _safe_reg(cls, errors)


def _bump_organizer_last(errors):
    cls = GNT_PT_organizer
    if getattr(cls, 'is_registered', False):
        _move_cat_to_end([cls], errors)


def visible_order():
    """Saved order filtered down to categories that exist right now."""
    st = space_state()
    roots_by_cat, _children = build_snapshot()
    hidden = set(st["hidden"])
    return [c for c in st["order"]
            if c not in hidden and (c in roots_by_cat or c in _hidden_cache)]


def apply_layout(order_override=None, save=True):
    """Make reality match the saved (or overridden) layout.

    Uses a move-to-end pass per category, so panels are never left
    unregistered if something fails mid-way.  When the change is a pure
    append (typical after another addon enables), only the new categories are
    touched.
    """
    global _APPLYING, _current_applied, _apply_ms
    if _APPLYING or _SHUTDOWN:
        return None
    _APPLYING = True
    t0 = time.perf_counter()
    try:
        st = space_state()
        roots_by_cat, children = build_snapshot()
        live = list(roots_by_cat.keys())

        order = list(order_override) if order_override is not None else list(st["order"])
        for c in live:
            if c not in order:
                order.append(c)
        for c in _hidden_cache:
            if c not in order:
                order.append(c)
        if order_override is None:
            st["order"] = order

        hidden = set(st["hidden"])
        target = [c for c in order
                  if c not in hidden and (c in roots_by_cat or c in _hidden_cache)]
        errors = []

        # 1) hide pass: unregister freshly-hidden categories into the cache
        did_hide = False
        for cat in live:
            if cat in hidden:
                flat = _cat_flat(cat, roots_by_cat, children)
                for cls in reversed(flat):
                    _safe_unreg(cls)
                _hidden_cache[cat] = flat
                did_hide = True
            elif cat in _hidden_cache:
                # live classes win over a stale cache entry (addon re-enabled)
                del _hidden_cache[cat]

        # 2) order pass
        cur = _current_applied
        pure_append = (not did_hide and cur is not None
                       and len(target) >= len(cur)
                       and target[:len(cur)] == cur
                       and all(c in roots_by_cat for c in target[len(cur):]))
        seq = target[len(cur):] if pure_append else target

        if seq:
            for cat in seq:
                if cat in roots_by_cat:
                    flat = _cat_flat(cat, roots_by_cat, children)
                else:
                    flat = _hidden_cache.pop(cat, [])
                _move_cat_to_end(flat, errors)
            _bump_organizer_last(errors)
        elif did_hide:
            _bump_organizer_last(errors)

        _current_applied = list(target)
        if save and order_override is None:
            save_state()
        _redraw_sidebars()
        if errors:
            _log("apply finished with", len(errors), "error(s)")
        return errors
    except Exception:
        traceback.print_exc()
        return None
    finally:
        _apply_ms = (time.perf_counter() - t0) * 1000.0
        _APPLYING = False


def _redraw_sidebars():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == SPACE:
                area.tag_redraw()


def sync_from_display(region=None):
    """Adopt the freshly-drawn real tab order if it disagrees with ours.

    Called at every strip interaction, where the region has just drawn, so the
    runtime enum is guaranteed current.  Keeps hidden/ghost entries anchored
    behind the visible neighbour they used to follow.  If reality disagrees in
    ways that need enforcement (a hidden tab resurfaced, an unknown tab
    appeared - e.g. registered through a cached pre-hook reference), a
    re-apply is scheduled: the addon self-heals on interaction.
    """
    global _current_applied
    disp = read_display_order(region)
    if not disp:
        return
    st = space_state()
    hidden = set(st["hidden"])
    known = set(st["order"])
    expected = [c for c in st["order"] if c not in hidden and c in set(disp)]
    needs_apply = any(c in hidden for c in disp) or any(c not in known for c in disp)
    if disp != expected:
        st["order"] = _rebuild_full_order(st["order"], disp,
                                          set(disp) | set(expected))
        save_state()
    _current_applied = list(disp)
    if needs_apply:
        _schedule_apply(0.05)


def _rebuild_full_order(old_full, new_visible, visible_set):
    """Rebuild the full saved order around a new visible arrangement, keeping
    invisible entries (hidden tabs, tabs of disabled addons) attached to the
    visible neighbour they previously followed."""
    trailing = {}          # visible cat -> [invisible cats that followed it]
    leading = []           # invisible cats before any visible one
    anchor = None
    for cat in old_full:
        if cat in visible_set and cat in new_visible:
            anchor = cat
        elif anchor is None:
            leading.append(cat)
        else:
            trailing.setdefault(anchor, []).append(cat)
    out = list(leading)
    for cat in new_visible:
        out.append(cat)
        out.extend(trailing.get(cat, ()))
    for cat in old_full:            # anything orphaned still tags along
        if cat not in out:
            out.append(cat)
    return out


# ---------------------------------------------------------------------------
# Registration interception (the "it just works" part)
# ---------------------------------------------------------------------------

def _is_managed_panel(cls):
    try:
        if not (isinstance(cls, type) and issubclass(cls, bpy.types.Panel)):
            return False
    except Exception:
        return False
    if cls is GNT_PT_organizer:
        return False
    if getattr(cls, 'bl_region_type', None) != 'UI':
        return False
    if getattr(cls, 'bl_space_type', None) != SPACE:
        return False
    return True


def _schedule_apply(delay=0.35):
    global _schedule_pending
    if _schedule_pending or _SHUTDOWN:
        return
    _schedule_pending = True

    def _cb():
        global _schedule_pending
        _schedule_pending = False
        if not _SHUTDOWN:
            try:
                apply_layout()
            except Exception:
                traceback.print_exc()
        return None

    try:
        bpy.app.timers.register(_cb, first_interval=delay)
    except Exception:
        _schedule_pending = False


def _note_external_register(cls):
    """A panel of another addon just registered.  If it belongs to a category
    that already has a tab (or is a sub-panel / hidden category), the real
    strip order silently changed, so our belief about it must be dropped -
    the next apply then does a full pass instead of an append-only one."""
    global _current_applied
    if _current_applied is not None:
        try:
            cat = getattr(cls, 'bl_category', '') or ''
            if (getattr(cls, 'bl_parent_id', '') or not cat
                    or cat in _current_applied
                    or cat in space_state()["hidden"]):
                _current_applied = None
        except Exception:
            _current_applied = None
    _schedule_apply()


def _cache_discard(cls):
    """If cls is one of *our* hidden (unregistered) classes, forget it and
    report True so the owner addon's unregister_class call succeeds silently."""
    if getattr(cls, 'is_registered', False):
        return False
    for cat, classes in list(_hidden_cache.items()):
        if cls in classes:
            classes.remove(cls)
            if not classes:
                del _hidden_cache[cat]
            return True
    return False


def _install_hooks():
    _remove_hooks()  # tolerate re-enable without a clean disable

    orig_reg = bpy.utils.register_class
    orig_unreg = bpy.utils.unregister_class
    alive = {'on': True}

    def gnt_register_class(cls, *args, **kwargs):
        result = orig_reg(cls, *args, **kwargs)
        try:
            if alive['on'] and not _APPLYING and _is_managed_panel(cls):
                _note_external_register(cls)
        except Exception:
            pass
        return result

    def gnt_unregister_class(cls, *args, **kwargs):
        try:
            if alive['on'] and not _APPLYING and _cache_discard(cls):
                return None
        except Exception:
            pass
        return orig_unreg(cls, *args, **kwargs)

    gnt_register_class._gnt_orig = orig_reg
    gnt_register_class._gnt_alive = alive
    gnt_unregister_class._gnt_orig = orig_unreg
    gnt_unregister_class._gnt_alive = alive
    bpy.utils.register_class = gnt_register_class
    bpy.utils.unregister_class = gnt_unregister_class


def _remove_hooks():
    for attr in ("register_class", "unregister_class"):
        fn = getattr(bpy.utils, attr)
        if hasattr(fn, '_gnt_orig'):
            fn._gnt_alive['on'] = False
            if getattr(bpy.utils, attr) is fn:
                setattr(bpy.utils, attr, fn._gnt_orig)


# ---------------------------------------------------------------------------
# Tab-strip geometry (for hit-testing and overlay drawing)
# ---------------------------------------------------------------------------

def _ui_scale():
    prefs = bpy.context.preferences
    return (prefs.system.dpi / 72.0) * prefs.system.pixel_size


def _tab_font_px():
    style = bpy.context.preferences.ui_styles[0]
    return max(6.0, style.widget.points * _ui_scale())


def strip_geometry(region, cats):
    """(x0, x1, rects) in region pixels; rects = [(cat, y_bottom, y_top), ...]
    top-to-bottom.  Tabs sit on the outer edge of the sidebar."""
    s = _ui_scale()
    strip_w = round(21 * s)
    if region.alignment == 'LEFT':
        x0, x1 = 0, strip_w
    else:
        x0, x1 = region.width - strip_w, region.width
    blf.size(0, _tab_font_px())
    
    # --- FIX 1: PADDING OVERHAUL ---
    # Reduced from 23 to 16 to accurately map Blender's geometric hitboxes. 
    pad = round(16 * s)
    y = region.height - round(4 * s)
    
    rects = []
    for cat in cats:
        h = blf.dimensions(0, cat)[0] + pad
        rects.append((cat, y - h, y))
        y -= h
    return x0, x1, rects


def _hit_tab(rects, y):
    for i, (_cat, y0, y1) in enumerate(rects):
        if y0 <= y <= y1:
            return i
    return -1


def _theme_tab_colors():
    ui = bpy.context.preferences.themes[0].user_interface
    wt = ui.wcol_tab
    inner_sel = tuple(wt.inner_sel)
    text_sel = tuple(wt.text_sel) + (1.0,)
    if len(inner_sel) == 3:
        inner_sel = inner_sel + (1.0,)
    return inner_sel, text_sel


# ---------------------------------------------------------------------------
# GPU overlay drawing
# ---------------------------------------------------------------------------

def _draw_quad(shader, x0, y0, x1, y1, color):
    batch = batch_for_shader(shader, 'TRIS',
                             {"pos": ((x0, y0), (x1, y0), (x1, y1),
                                      (x0, y0), (x1, y1), (x0, y1))})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


_icon_cache = {}


def _icon(*candidates):
    """First icon name that exists in this Blender build."""
    key = candidates
    if key in _icon_cache:
        return _icon_cache[key]
    try:
        valid = _icon.valid
    except AttributeError:
        try:
            items = bpy.types.UILayout.bl_rna.functions["prop"] \
                .parameters["icon"].enum_items
            valid = {i.identifier for i in items}
        except Exception:
            valid = None
        _icon.valid = valid
    for name in candidates:
        if valid is None or name in valid:
            _icon_cache[key] = name
            return name
    _icon_cache[key] = 'NONE'
    return 'NONE'


def _draw_tab_drag_overlay(op):
    try:
        if bpy.context.region != op.region:
            return
        s = _ui_scale()
        inner_sel, text_sel = _theme_tab_colors()
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')

        x0, x1 = op.strip_x0, op.strip_x1

        # insertion indicator (skip in live mode: the strip itself reorders)
        if not op.live and op.indicator_y is not None:
            th = max(2.0, 2.0 * s)
            _draw_quad(shader, x0 + 1, op.indicator_y - th * 0.5,
                       x1 - 1, op.indicator_y + th * 0.5, text_sel)

        # ghost tab following the cursor
        gy = op.mouse_y
        h = op.grab_h
        alpha = 0.45 if op.live else 0.85
        fill = (inner_sel[0], inner_sel[1], inner_sel[2], alpha)
        _draw_quad(shader, x0, gy - h * 0.5, x1, gy + h * 0.5, fill)

        blf.size(0, _tab_font_px())
        tw, th_txt = blf.dimensions(0, op.grab_cat)
        blf.enable(0, blf.ROTATION)
        blf.rotation(0, math.pi * 0.5)
        blf.color(0, text_sel[0], text_sel[1], text_sel[2],
                  0.7 if op.live else 1.0)
        blf.position(0, x0 + (x1 - x0) * 0.5 + th_txt * 0.5,
                     gy - tw * 0.5, 0)
        blf.draw(0, op.grab_cat)
        blf.disable(0, blf.ROTATION)
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _draw_row_drag_overlay(op):
    try:
        if bpy.context.region != op.region:
            return
        inner_sel, text_sel = _theme_tab_colors()
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        s = _ui_scale()
        blf.size(0, _tab_font_px())
        label = op.carry_cat
        tw, th = blf.dimensions(0, label)
        pad = 6 * s
        x = op.mouse_x + 16 * s
        y = op.mouse_y - th * 0.5
        fill = (inner_sel[0], inner_sel[1], inner_sel[2], 0.9)
        _draw_quad(shader, x - pad, y - pad, x + tw + pad, y + th + pad, fill)
        blf.color(0, text_sel[0], text_sel[1], text_sel[2], 1.0)
        blf.position(0, x, y, 0)
        blf.draw(0, label)
        gpu.state.blend_set('NONE')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shared edit helpers
# ---------------------------------------------------------------------------

def _live_mode_available():
    return _apply_ms is not None and _apply_ms < LIVE_APPLY_BUDGET_MS


def _commit_visible_reorder(new_visible):
    st = space_state()
    st["order"] = _rebuild_full_order(st["order"], new_visible,
                                      set(new_visible))
    save_state()
    apply_layout()


def _on_strip(context, event):
    region = context.region
    if region is None or region.type != 'UI':
        return None
    if context.area is None or context.area.type != SPACE:
        return None
        
    # --- FIX 2: REALITY OVERRIDE ---
    # Force the hit test to only use Blender's confirmed visual layout, 
    # preventing natively hidden tabs from leaving invisible empty space.
    global _current_applied
    cats = _current_applied if _current_applied is not None else visible_order()
    
    if not cats:
        return None
    x0, x1, rects = strip_geometry(region, cats)
    s = _ui_scale()
    if not (x0 - 4 * s <= event.mouse_region_x <= x1):
        return None
    return region, cats, (x0, x1, rects)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class GNT_OT_tab_drag(bpy.types.Operator):
    """Move a sidebar tab: Ctrl+drag it, or hover it and press G.
    Click or Enter drops it, Esc or right-click cancels"""
    bl_idname = "gnt.tab_drag"
    bl_label = "Move Sidebar Tab"
    bl_options = {'INTERNAL'}

    _handle = None

    def invoke(self, context, event):
        hit = None
        try:
            sync_from_display(context.region if context.region
                              and context.region.type == 'UI' else None)
            hit = _on_strip(context, event)
        except Exception:
            traceback.print_exc()
        if hit is None:
            return {'PASS_THROUGH'}
        region, cats, (x0, x1, rects) = hit
        idx = _hit_tab(rects, event.mouse_region_y)
        if idx < 0:
            return {'PASS_THROUGH'}

        st = space_state()
        cat = cats[idx]
        if cat in st["pinned"]:
            self.report({'INFO'},
                        "'%s' is pinned - unpin it to move it" % cat)
            return {'CANCELLED'}

        self.region = region
        self.start_cats = list(cats)
        self.work_cats = list(cats)
        self.grab_cat = cat
        self.grab_h = rects[idx][2] - rects[idx][1]
        self.strip_x0, self.strip_x1 = x0, x1
        self.mouse_y = event.mouse_region_y
        self.indicator_y = None
        self.live = _live_mode_available()
        self.moved = False

        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_tab_drag_overlay, (self,), 'UI', 'POST_PIXEL')
        context.window.cursor_modal_set('HAND')
        context.window_manager.modal_handler_add(self)
        if context.area:
            context.area.header_text_set(
                "Drag '%s'  |  release: drop   Esc/RMB: cancel" % cat)
        self._update(context, event)
        return {'RUNNING_MODAL'}

    def _slot_from_y(self, rects, y, skip_cat):
        """Insertion slot among self.work_cats without the grabbed tab."""
        slot = 0
        for cat, y0, y1 in rects:
            if cat == skip_cat:
                continue
            mid = (y0 + y1) * 0.5
            if y < mid:
                slot += 1
        return slot

    def _update(self, context, event):
        self.mouse_y = event.mouse_region_y
        if self.live and not _live_mode_available():
            self.live = False           # apply got slow - degrade to ghost mode
        cats_now = self.work_cats
        _x0, _x1, rects = strip_geometry(self.region, cats_now)
        slot = self._slot_from_y(rects, self.mouse_y, self.grab_cat)

        others = [c for c in cats_now if c != self.grab_cat]
        slot = max(0, min(slot, len(others)))
        proposed = others[:slot] + [self.grab_cat] + others[slot:]
        if proposed != cats_now:
            self.work_cats = proposed
            self.moved = True
            if self.live:
                apply_layout(order_override=_rebuild_full_order(
                    space_state()["order"], proposed, set(proposed)),
                    save=False)
        # indicator line position (ghost mode)
        if not self.live:
            _x0, _x1, rects0 = strip_geometry(self.region, self.start_cats)
            vis = [r for r in rects0 if r[0] != self.grab_cat]
            if slot == 0:
                self.indicator_y = rects0[0][2] if not vis else vis[0][2]
            elif slot - 1 < len(vis):
                self.indicator_y = vis[slot - 1][1]
            else:
                self.indicator_y = vis[-1][1] if vis else None
        self.region.tag_redraw()

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            self._update(context, event)
            return {'RUNNING_MODAL'}
        commit = (event.type == 'LEFTMOUSE' and event.value == 'RELEASE') \
            or (event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}
                and event.value == 'PRESS')
        if commit:
            self._finish(context)
            if self.moved and self.work_cats != self.start_cats:
                _commit_visible_reorder(self.work_cats)
            elif self.live and self.moved:
                apply_layout()          # restore saved order
            try:
                self.region.active_panel_category = self.grab_cat
            except Exception:
                pass
            return {'FINISHED'}
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            self._finish(context)
            if self.live and self.moved:
                apply_layout()          # revert live preview
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        if self._handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'UI')
            self._handle = None
        context.window.cursor_modal_restore()
        if context.area:
            context.area.header_text_set(None)
        self.region.tag_redraw()


class GNT_OT_tab_context(bpy.types.Operator):
    """Open the tab context menu"""
    bl_idname = "gnt.tab_context"
    bl_label = "Sidebar Tab Menu"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        global _ctx_menu_target
        hit = None
        try:
            sync_from_display(context.region if context.region
                              and context.region.type == 'UI' else None)
            hit = _on_strip(context, event)
        except Exception:
            traceback.print_exc()
        if hit is None:
            return {'PASS_THROUGH'}
        _region, cats, (_x0, _x1, rects) = hit
        idx = _hit_tab(rects, event.mouse_region_y)
        if idx < 0:
            return {'PASS_THROUGH'}
        _ctx_menu_target = cats[idx]
        bpy.ops.wm.call_menu(name=GNT_MT_tab_context.__name__)
        return {'FINISHED'}


class GNT_OT_hide_tab(bpy.types.Operator):
    """Hide this tab (its panels stay loaded, just tucked away)"""
    bl_idname = "gnt.hide_tab"
    bl_label = "Hide Tab"
    bl_options = {'INTERNAL'}

    category: bpy.props.StringProperty()

    def execute(self, context):
        st = space_state()
        cat = self.category
        if cat in st["pinned"]:
            self.report({'WARNING'}, "'%s' is pinned and can't be hidden" % cat)
            return {'CANCELLED'}
        if cat not in st["hidden"]:
            if len(visible_order()) <= 1:
                self.report({'WARNING'}, "Can't hide the last visible tab")
                return {'CANCELLED'}
            st["hidden"].append(cat)
            save_state()
            apply_layout()
            self.report({'INFO'},
                        "Hidden '%s'   (Alt+H: show all,  M: menu,  ≡ tab)" % cat)
        return {'FINISHED'}


class GNT_OT_show_tab(bpy.types.Operator):
    """Show this hidden tab again"""
    bl_idname = "gnt.show_tab"
    bl_label = "Show Tab"
    bl_options = {'INTERNAL'}

    category: bpy.props.StringProperty()

    def execute(self, context):
        st = space_state()
        if self.category in st["hidden"]:
            st["hidden"].remove(self.category)
            save_state()
            apply_layout()
        return {'FINISHED'}


class GNT_OT_show_all(bpy.types.Operator):
    """Unhide every hidden tab"""
    bl_idname = "gnt.show_all"
    bl_label = "Show All Tabs"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        st = space_state()
        if st["hidden"]:
            st["hidden"].clear()
            save_state()
            apply_layout()
        return {'FINISHED'}


class GNT_OT_toggle_pin(bpy.types.Operator):
    """Pin/unpin this tab (pinned tabs can't be dragged or hidden)"""
    bl_idname = "gnt.toggle_pin"
    bl_label = "Pin Tab"
    bl_options = {'INTERNAL'}

    category: bpy.props.StringProperty()

    def execute(self, context):
        st = space_state()
        if self.category in st["pinned"]:
            st["pinned"].remove(self.category)
        else:
            st["pinned"].append(self.category)
            if self.category in st["hidden"]:
                st["hidden"].remove(self.category)
                apply_layout()
        save_state()
        _redraw_sidebars()
        return {'FINISHED'}


class GNT_OT_move_tab(bpy.types.Operator):
    """Move this tab one step (keyboard-friendly alternative to dragging)"""
    bl_idname = "gnt.move_tab"
    bl_label = "Move Tab"
    bl_options = {'INTERNAL'}

    category: bpy.props.StringProperty()
    delta: bpy.props.IntProperty(default=1)

    def execute(self, context):
        st = space_state()
        if self.category in st["pinned"]:
            self.report({'INFO'}, "'%s' is pinned" % self.category)
            return {'CANCELLED'}
        cats = visible_order()
        if self.category not in cats:
            return {'CANCELLED'}
        i = cats.index(self.category)
        j = max(0, min(len(cats) - 1, i + self.delta))
        if i == j:
            return {'CANCELLED'}
        cats.insert(j, cats.pop(i))
        _commit_visible_reorder(cats)
        return {'FINISHED'}


class GNT_OT_forget_tab(bpy.types.Operator):
    """Forget this tab (it belongs to a disabled addon).  If the addon comes
    back, its tab simply appears at the end again"""
    bl_idname = "gnt.forget_tab"
    bl_label = "Forget Tab"
    bl_options = {'INTERNAL'}

    category: bpy.props.StringProperty()

    def execute(self, context):
        st = space_state()
        for key in ("order", "hidden", "pinned"):
            if self.category in st[key]:
                st[key].remove(self.category)
        save_state()
        _redraw_sidebars()
        return {'FINISHED'}


class GNT_OT_reset(bpy.types.Operator):
    """Clear all saved ordering, hidden and pinned state"""
    bl_idname = "gnt.reset"
    bl_label = "Reset Grab N Tab layout?"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        global _current_applied
        st = space_state()
        st["order"] = []
        st["hidden"].clear()
        st["pinned"].clear()
        save_state()
        # bring every cached tab back and adopt whatever order results
        errors = []
        for cat in list(_hidden_cache):
            for cls in _hidden_cache.pop(cat):
                _safe_reg(cls, errors)
        _current_applied = None
        st["order"] = read_display_order() or []
        apply_layout()
        self.report({'INFO'}, "Grab N Tab layout reset")
        return {'FINISHED'}


class GNT_OT_row_drag(bpy.types.Operator):
    """Click to pick this tab up, move the mouse, click again to drop.
    Esc / right-click cancels"""
    bl_idname = "gnt.row_drag"
    bl_label = "Reorder Tab"
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty()

    _handle = None

    def invoke(self, context, event):
        cats = _organizer_rows()
        if not (0 <= self.index < len(cats)):
            return {'CANCELLED'}
        cat = cats[self.index][0]
        st = space_state()
        if cat in st["pinned"]:
            self.report({'INFO'}, "'%s' is pinned - unpin it to move it" % cat)
            return {'CANCELLED'}
        vis = visible_order()
        if cat not in vis:
            self.report({'INFO'}, "Unhide '%s' before reordering it" % cat)
            return {'CANCELLED'}

        self.region = context.region
        self.carry_cat = cat
        self.start_vis = list(vis)
        self.work_vis = list(vis)
        self.anchor_y = event.mouse_region_y
        self.mouse_x = event.mouse_region_x
        self.mouse_y = event.mouse_region_y
        self.row_h = max(8.0, 20.0 * _ui_scale())
        self.live = _live_mode_available()
        self.moved = False

        if self.region is not None:
            self._handle = bpy.types.SpaceView3D.draw_handler_add(
                _draw_row_drag_overlay, (self,), 'UI', 'POST_PIXEL')
        context.window.cursor_modal_set('SCROLL_Y')
        if context.area:
            context.area.header_text_set(
                "Moving '%s'  |  click: drop   Esc/RMB: cancel" % cat)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _apply_offset(self, context):
        vis = self.work_vis
        i = vis.index(self.carry_cat)
        steps = int(round((self.anchor_y - self.mouse_y) / self.row_h))
        if steps == 0:
            return
        j = max(0, min(len(vis) - 1, i + steps))
        if i == j:
            return
        vis.insert(j, vis.pop(i))
        self.anchor_y -= steps * self.row_h
        self.moved = True
        if self.live:
            apply_layout(order_override=_rebuild_full_order(
                space_state()["order"], vis, set(vis)), save=False)
        if context.area:
            context.area.tag_redraw()

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            self.mouse_x = event.mouse_region_x
            self.mouse_y = event.mouse_region_y
            self._apply_offset(context)
            if self.region is not None:
                self.region.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value in {'PRESS', 'CLICK'}:
            self._finish(context)
            if self.moved and self.work_vis != self.start_vis:
                _commit_visible_reorder(self.work_vis)
            elif self.live and self.moved:
                apply_layout()
            return {'FINISHED'}
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            self._finish(context)
            if self.live and self.moved:
                apply_layout()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        if self._handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'UI')
            self._handle = None
        context.window.cursor_modal_restore()
        if context.area:
            context.area.header_text_set(None)
            context.area.tag_redraw()


class GNT_OT_hide_hover(bpy.types.Operator):
    """Hide the sidebar tab under the cursor"""
    bl_idname = "gnt.hide_hover"
    bl_label = "Hide Hovered Tab"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        hit = None
        try:
            sync_from_display(context.region if context.region
                              and context.region.type == 'UI' else None)
            hit = _on_strip(context, event)
        except Exception:
            traceback.print_exc()
        if hit is None:
            return {'PASS_THROUGH'}
        _region, cats, (_x0, _x1, rects) = hit
        idx = _hit_tab(rects, event.mouse_region_y)
        if idx < 0:
            return {'PASS_THROUGH'}
        cat = cats[idx]
        st = space_state()
        if cat in st["pinned"]:
            self.report({'WARNING'}, "'%s' is pinned and can't be hidden" % cat)
            return {'CANCELLED'}
        if len(visible_order()) <= 1:
            self.report({'WARNING'}, "Can't hide the last visible tab")
            return {'CANCELLED'}
        if cat not in st["hidden"]:
            st["hidden"].append(cat)
            save_state()
            apply_layout()
        self.report({'INFO'},
                    "Hidden '%s'   (Alt+H: show all,  M: menu,  ≡ tab)" % cat)
        return {'FINISHED'}


class GNT_OT_show_all_hover(bpy.types.Operator):
    """Unhide every hidden tab (hover the tab strip)"""
    bl_idname = "gnt.show_all_hover"
    bl_label = "Show All Tabs"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        hit = None
        try:
            sync_from_display(context.region if context.region
                              and context.region.type == 'UI' else None)
            hit = _on_strip(context, event)
        except Exception:
            traceback.print_exc()
        if hit is None:
            return {'PASS_THROUGH'}
        st = space_state()
        n = len(st["hidden"])
        if not n:
            self.report({'INFO'}, "No hidden tabs")
            return {'CANCELLED'}
        st["hidden"].clear()
        save_state()
        apply_layout()
        self.report({'INFO'}, "Unhid %d tab%s" % (n, "s" if n > 1 else ""))
        return {'FINISHED'}


class GNT_OT_force_apply(bpy.types.Operator):
    """Re-sync and re-apply the saved layout right now"""
    bl_idname = "gnt.force_apply"
    bl_label = "Re-apply Layout"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        sync_from_display()
        apply_layout()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------

class GNT_MT_unhide(bpy.types.Menu):
    bl_idname = "GNT_MT_unhide"
    bl_label = "Unhide"

    def draw(self, context):
        layout = self.layout
        hidden = space_state()["hidden"]
        if not hidden:
            layout.label(text="No hidden tabs")
            return
        for cat in hidden:
            layout.operator("gnt.show_tab", text=cat,
                            icon='HIDE_OFF').category = cat
        layout.separator()
        layout.operator("gnt.show_all", icon='RESTRICT_VIEW_OFF')


class GNT_MT_tab_context(bpy.types.Menu):
    bl_idname = "GNT_MT_tab_context"
    bl_label = "Sidebar Tab"

    def draw(self, context):
        layout = self.layout
        cat = _ctx_menu_target
        st = space_state()
        pinned = cat in st["pinned"]

        layout.label(text=cat, icon='MENU_PANEL')
        layout.separator()
        row = layout.column()
        row.enabled = not pinned
        op = row.operator("gnt.hide_tab", text="Hide", icon='HIDE_ON')
        op.category = cat
        layout.operator("gnt.toggle_pin",
                        text="Unpin" if pinned else "Pin",
                        icon='PINNED' if pinned else 'UNPINNED').category = cat
        col = layout.column()
        col.enabled = not pinned
        op = col.operator("gnt.move_tab", text="Move Up", icon='TRIA_UP')
        op.category, op.delta = cat, -1
        op = col.operator("gnt.move_tab", text="Move Down", icon='TRIA_DOWN')
        op.category, op.delta = cat, 1
        layout.separator()
        layout.menu("GNT_MT_unhide", icon='HIDE_OFF')


# ---------------------------------------------------------------------------
# Organizer panel (the slim '≡' tab)
# ---------------------------------------------------------------------------

def _organizer_rows():
    """[(category, is_live, is_hidden, is_pinned), ...] in saved order."""
    st = space_state()
    roots_by_cat, _children = build_snapshot()
    hidden = set(st["hidden"])
    pinned = set(st["pinned"])
    rows = []
    for cat in st["order"]:
        live = cat in roots_by_cat or cat in _hidden_cache
        rows.append((cat, live, cat in hidden, cat in pinned))
    return rows


class GNT_PT_organizer(bpy.types.Panel):
    bl_space_type = SPACE
    bl_region_type = 'UI'
    bl_category = ORGANIZER_CAT
    bl_label = "Grab N Tab"

    def draw(self, context):
        layout = self.layout
        rows = _organizer_rows()
        if not rows:
            layout.label(text="No tabs found yet")
            return
        col = layout.column(align=True)
        for i, (cat, live, hidden, pinned) in enumerate(rows):
            row = col.row(align=True)
            if not live:
                sub = row.row(align=True)
                sub.active = False
                sub.label(text=cat, icon='GHOST_DISABLED')
                row.operator("gnt.forget_tab", text="",
                             icon='X').category = cat
                continue
            grip = row.row(align=True)
            grip.active = not hidden
            op = grip.operator("gnt.row_drag", text=cat,
                               icon=_icon('GRIP', 'VIEW_PAN', 'NONE'))
            op.index = i
            pin = row.row(align=True)
            pin.operator("gnt.toggle_pin", text="",
                         icon='PINNED' if pinned else 'UNPINNED',
                         depress=pinned).category = cat
            eye = row.row(align=True)
            eye.enabled = not pinned
            if hidden:
                eye.operator("gnt.show_tab", text="",
                             icon='HIDE_ON', depress=True).category = cat
            else:
                eye.operator("gnt.hide_tab", text="",
                             icon='HIDE_OFF').category = cat
        layout.separator()
        row = layout.row(align=True)
        row.operator("gnt.force_apply", text="Re-apply", icon='FILE_REFRESH')
        row.operator("gnt.reset", text="Reset", icon='LOOP_BACK')
        col = layout.column(align=True)
        col.active = False
        col.scale_y = 0.8
        col.label(text="On the tab strip:")
        col.label(text="Ctrl+Drag or G  -  move tab")
        col.label(text="H hide   Alt+H show all")
        col.label(text="M  -  tab menu")


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

def _rebuild_keymaps(_self=None, _context=None):
    try:
        _unregister_keymaps()
        _register_keymaps()
    except Exception:
        traceback.print_exc()


def _toggle_organizer_tab(_self=None, _context=None):
    try:
        prefs = _prefs()
        registered = getattr(GNT_PT_organizer, 'is_registered', False)
        if prefs.show_organizer_tab and not registered:
            _orig_register()(GNT_PT_organizer)
        elif not prefs.show_organizer_tab and registered:
            _orig_unregister()(GNT_PT_organizer)
        _redraw_sidebars()
    except Exception:
        traceback.print_exc()


class GNTPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    enable_direct_drag: bpy.props.BoolProperty(
        name="Move gestures on the strip (Ctrl+Drag, G)",
        description="Ctrl+drag a sidebar tab, or hover it and press G, "
                    "to move it (plain click keeps switching tabs)",
        default=True, update=_rebuild_keymaps)
    enable_context_menu: bpy.props.BoolProperty(
        name="Quick keys on the strip (M menu, H hide, Alt+H show all)",
        description="Hover a sidebar tab and press M for its menu, "
                    "H to hide it, Alt+H to unhide everything",
        default=True, update=_rebuild_keymaps)
    show_organizer_tab: bpy.props.BoolProperty(
        name="Show the '≡' organizer tab",
        description="A slim last tab hosting the tab organizer",
        default=True, update=_toggle_organizer_tab)

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "enable_direct_drag")
        col.prop(self, "enable_context_menu")
        col.prop(self, "show_organizer_tab")
        box = layout.box()
        st = space_state()
        box.label(text="Saved layout: %d tabs, %d hidden, %d pinned"
                       % (len(st["order"]), len(st["hidden"]),
                          len(st["pinned"])), icon='FILE_CACHE')
        box.label(text=_config_path())
        row = box.row(align=True)
        row.operator("gnt.force_apply", icon='FILE_REFRESH')
        row.operator("gnt.reset", icon='LOOP_BACK')


def _prefs():
    return bpy.context.preferences.addons[__name__].preferences


# ---------------------------------------------------------------------------
# Keymaps
# ---------------------------------------------------------------------------

def _register_keymaps():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    try:
        prefs = _prefs()
        want_drag = prefs.enable_direct_drag
        want_menu = prefs.enable_context_menu
    except Exception:
        want_drag = want_menu = True
    # NOTE: plain LMB / RMB (with or without Alt) never reach the keymap layer
    # over the category tabs - Blender's UI button handler consumes them
    # first (verified empirically on 5.2 with injected input).  Ctrl-modified
    # clicks and plain keyboard keys DO fall through, so those are the
    # gestures that can exist:
    km = kc.keymaps.new(name='3D View Generic', space_type='VIEW_3D')
    if want_drag:
        kmi = km.keymap_items.new("gnt.tab_drag", 'LEFTMOUSE', 'CLICK_DRAG',
                                  ctrl=True)
        _keymap_items.append((km, kmi))
        kmi = km.keymap_items.new("gnt.tab_drag", 'G', 'PRESS')
        _keymap_items.append((km, kmi))
    if want_menu:
        kmi = km.keymap_items.new("gnt.tab_context", 'M', 'PRESS')
        _keymap_items.append((km, kmi))
        kmi = km.keymap_items.new("gnt.hide_hover", 'H', 'PRESS')
        _keymap_items.append((km, kmi))
        kmi = km.keymap_items.new("gnt.show_all_hover", 'H', 'PRESS',
                                  alt=True)
        _keymap_items.append((km, kmi))


def _unregister_keymaps():
    for km, kmi in _keymap_items:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _keymap_items.clear()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _startup_timer():
    global _seed_tries, _current_applied
    if _SHUTDOWN:
        return None
    _seed_tries += 1
    disp = read_display_order()
    if not disp and _seed_tries < 8:
        return 0.6                      # region not drawn yet - retry
    try:
        st = space_state()
        if not st["order"]:
            st["order"] = disp or [c for c in build_snapshot()[0]]
            save_state()
        if _current_applied is None and disp:
            _current_applied = [c for c in disp
                                if c not in set(st["hidden"])]
        apply_layout()
    except Exception:
        traceback.print_exc()
    return None


@bpy.app.handlers.persistent
def _on_load_post(dummy=None):
    global _current_applied, _seed_tries
    _current_applied = None
    _seed_tries = 0
    load_state()
    if not bpy.app.timers.is_registered(_startup_timer):
        bpy.app.timers.register(_startup_timer, first_interval=0.4)


_classes = (
    GNT_OT_tab_drag,
    GNT_OT_tab_context,
    GNT_OT_hide_tab,
    GNT_OT_show_tab,
    GNT_OT_show_all,
    GNT_OT_toggle_pin,
    GNT_OT_move_tab,
    GNT_OT_forget_tab,
    GNT_OT_reset,
    GNT_OT_row_drag,
    GNT_OT_hide_hover,
    GNT_OT_show_all_hover,
    GNT_OT_force_apply,
    GNT_MT_unhide,
    GNT_MT_tab_context,
    GNTPreferences,
)


def register():
    global _SHUTDOWN, _seed_tries, _current_applied
    _SHUTDOWN = False
    _seed_tries = 0
    _current_applied = None
    load_state()
    for cls in _classes:
        bpy.utils.register_class(cls)
    _install_hooks()
    try:
        if _prefs().show_organizer_tab:
            _orig_register()(GNT_PT_organizer)
    except Exception:
        _orig_register()(GNT_PT_organizer)
    _register_keymaps()
    bpy.app.timers.register(_startup_timer, first_interval=0.4)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    global _SHUTDOWN, _APPLYING
    _SHUTDOWN = True
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    try:
        if bpy.app.timers.is_registered(_startup_timer):
            bpy.app.timers.unregister(_startup_timer)
    except Exception:
        pass
    _unregister_keymaps()
    # bring hidden tabs back before we go
    _APPLYING = True
    try:
        errors = []
        for cat in list(_hidden_cache):
            for cls in _hidden_cache.pop(cat):
                _safe_reg(cls, errors)
    finally:
        _APPLYING = False
    _remove_hooks()
    if getattr(GNT_PT_organizer, 'is_registered', False):
        _orig_unregister()(GNT_PT_organizer)
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    save_state()
    _redraw_sidebars()


if __name__ == "__main__":
    register()