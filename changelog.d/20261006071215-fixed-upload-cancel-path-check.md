- **Cancelling a knowledge upload can no longer delete a file outside the upload
  folder.** The cancel action deleted whatever path the upload's database row
  held, without checking that it was still inside the upload directory. It now
  removes only the upload's own file, addressed by name inside the upload's own
  directory and never through a symlink, and refuses (409) while leaving the
  file and the row alone if the row points anywhere else. The dashboard's Cancel
  button now shows that refusal instead of clearing the dialog as if the cancel
  had worked.
