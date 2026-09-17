- The one-click fleet door now opens a fresh session picker every time, and a second
  fleet window no longer takes over the first one. A tmux pane mode belongs to the pane
  rather than the client, so the picker survived the terminal window closing — the next
  connect landed back inside the previous chooser, showing another session's preview
  with keystrokes going to the picker instead of the app. And because every window
  attached to the same landing session, opening a second one reset the pane under the
  first, ending whatever was running there. The door is now a script that resets the
  pane only when nobody is attached, and hands a concurrent window its own short-lived
  session instead. The numbered slots are untouched either way.
