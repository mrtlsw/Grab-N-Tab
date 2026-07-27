# Grab N Tab

Drag-and-drop reordering, hiding and pinning of Blender's 3D-Viewport
N-panel (sidebar) tabs. Your layout persists automatically across sessions
and .blend files — no "apply" buttons, no per-addon mapping, no stale lists.

Built and tested on **Blender 5.2 LTS** (minimum 4.2). Author: **mrtlsw**.

## Installation

1. Download `grab_n_tab.py`.
2. In Blender: `Edit → Preferences → Add-ons → Install from Disk…`, pick the
   file, then enable **Grab N Tab**.

A slim **≡** tab appears at the end of the 3D Viewport sidebar — that's the
organizer.

## Using it

All gestures happen **on the tab strip** (plain click still switches tabs,
exactly as before):

| Action | How |
|---|---|
| Move a tab | **Ctrl+drag the tab itself** (tabs shuffle live under the cursor), or hover it and press **`G`**, move, click/Enter to drop. Also: drag rows in the **≡** organizer tab (click a row to pick it up, move, click to drop) |
| Hide a tab | Hover it, press **`H`** — or the eye icon in **≡**, or `M` → Hide |
| Unhide | **`Alt+H`** over the strip shows all; individually via **`M`** → Unhide ▸ or the eye icons in **≡** |
| Tab menu | Hover a tab, press **`M`** (hide / pin / move / unhide list) |
| Pin a tab | `M` → Pin (pinned tabs can't be moved or hidden) |
| Cancel a move | `Esc` or right-click while dragging |
| Reset everything | **≡** tab → Reset, or addon preferences |

> Why not plain left-drag or right-click on tabs? Blender's UI button
> handler consumes plain LMB/RMB on the category tabs before any addon
> keymap can see them (verified by injecting raw input on 5.2 and logging
> what reaches the keymap layer — not even `PRESS` arrives). Ctrl-modified
> clicks and plain keys fall through, so those are the gestures that can
> exist without patching Blender itself.

Your layout is saved instantly to `grab_n_tab/state.json` inside Blender's
per-user config directory (atomic writes; independent of "Save Preferences",
survives crashes, new files and restarts).

## Why it "just works" (implementation notes)

Most tab-manager addons make you map addons to categories by hand and press
refresh whenever anything changes. This one instead:

1. **Owns registration order.** Blender orders tabs by panel *registration*
   order, so the addon re-registers sidebar panels (dependency-aware —
   sub-panels always follow their parents) to match your saved order.
   A full pass over ~110 panels takes ~6 ms, which is fast enough to
   re-apply live *while you drag*.
2. **Reads the truth back from Blender.** Assigning an invalid value to
   `region.active_panel_category` raises an error listing every category in
   exact display order. The addon re-syncs from that at every interaction,
   so it can never drift from reality.
3. **Intercepts `bpy.utils.register_class` / `unregister_class`** while
   enabled. When any addon registers sidebar panels later (enable, disable,
   update, deferred registration), a debounced reconciler re-applies your
   order and hidden set automatically.
4. **Hidden tabs are cached, not lost.** Panels of a hidden tab are
   unregistered but kept; if their owner addon is disabled while hidden, the
   interception layer answers its cleanup calls so nothing errors, and if the
   addon comes back the tab re-hides itself.

Disabling Grab N Tab restores all hidden tabs and removes the hooks.

## Development

`grab_n_tab.py` is the whole addon. To iterate, re-install it over the
enabled copy:

```python
import bpy
bpy.ops.preferences.addon_disable(module='grab_n_tab')
bpy.ops.preferences.addon_install(filepath="/path/to/grab_n_tab.py", overwrite=True)
bpy.ops.preferences.addon_enable(module='grab_n_tab')
```

## License

GPL-3.0-or-later, like Blender itself.
