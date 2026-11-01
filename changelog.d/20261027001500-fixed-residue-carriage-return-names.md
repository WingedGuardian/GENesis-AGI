- The update-state guard reads residue paths as raw bytes, so a file name
  holding a carriage return is shown and quoted as itself instead of as a
  different name with a newline in it.
