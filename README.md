# SketchUp Importer for Blender

For Blender 4.2+ on Windows x64 and macOS with a matching SketchUp Desktop SDK architecture. Install the runtime-ready ZIP in Blender using **Preferences → Add-ons → Install from Disk**, then choose **File → Import → SketchUp (.skp)**.

Auto Join is the default and joins a subtree at 10 immediate children. Hierarchy retains parenting and No Join creates separate, unparented objects. Create Bounding Boxes is enabled by default and adds selectable edge-only boxes to top-level hierarchies with children.

## Package your own runtime ZIP

Start with the source ZIP from GitHub Actions. Extract it and add the files from your [SketchUp Desktop SDK](https://extensions.sketchup.com/en/developer_center/sketchup_sdk):

- Windows: put `SketchUpAPI.dll` and `SketchUpCommonPreferences.dll` from the x64 SDK beside each other in `skp_importer/x64/`.
- macOS: copy `SketchUpAPI.framework/Versions/A/` into `skp_importer/SketchUpAPI.framework/Versions/A/`. Include the `SketchUpAPI` binary and its `Libraries`, `Resources`, and `_CodeSignature` directories. Omit `Headers`. Use a framework matching the architecture of your Blender build.

Zip the `skp_importer` directory itself. The ZIP should contain `skp_importer/__init__.py`. Blender loads the runtime from the installed add-on automatically.

The source checkout and public workflow artifacts contain only the add-on code. Model compatibility depends on the installed SDK. Perspective-projected texture placement can still differ from SketchUp.
