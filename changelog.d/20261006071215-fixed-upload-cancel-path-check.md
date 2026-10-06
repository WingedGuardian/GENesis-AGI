- **Cancelling a knowledge upload can no longer delete a file outside the upload
  folder.** The cancel action deleted whatever path the upload's database row
  held, without checking that it was still inside the upload directory. It now
  refuses (409) and leaves the file and the row alone if the stored path points
  anywhere else, and never removes the upload directory itself.
