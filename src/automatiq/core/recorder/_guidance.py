"""Shared user-guidance strings (kept light: importable from package __init__)."""

# The System Settings fix steps, shared verbatim by the macOS screen-permission
# guidance in recorder/__init__._check_macos_screen_permission and
# video_recorder's capture-error path.
MACOS_PERMISSION_STEPS = (
    "  1. Open System Settings > Privacy & Security > Screen & System Audio Recording\n"
    "  2. Enable the toggle for your terminal app (Terminal, iTerm2, VS Code, etc.)\n"
)
