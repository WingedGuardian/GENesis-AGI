- The one-click fleet door now opens a fresh session picker every time. A tmux pane mode
  belongs to the pane rather than the client, so the picker survived the terminal window
  closing — the next connect landed back inside the previous chooser, showing another
  session's preview with keystrokes going to the picker instead of the app. It looked like
  being dropped into an unrecognised, frozen session, and it came and went depending on how
  the last visit ended. The door now resets its own pane before opening the picker; the
  numbered slots are untouched.
