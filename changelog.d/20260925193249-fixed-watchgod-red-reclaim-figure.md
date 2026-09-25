- The cc-tmp watchdog now reports how much space a RED cleanup actually
  reclaimed, instead of always logging "nuclear cleanup complete". When a pass
  frees nothing it says so and names the live processes holding descriptors
  under the directory, so a full temp dir that cleanup cannot fix is
  diagnosable from the log rather than requiring an investigation. The figure
  is read from filesystem free space rather than directory size, because a file
  unlinked while a process still holds it open leaves the directory listing
  immediately while its blocks stay allocated — the exact case that made the
  old line misleading.
