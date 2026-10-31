- **The merge gate reads CodeRabbit's current inline finding header again.** Since
  2026-10-17 CodeRabbit opens each inline finding with a marker comment, and since
  2026-10-25 its header draws the level's icon as HTML and separates fields with a
  middle dot (`**<icon> Major** · **Category** · **Effort**`). The gate read neither
  layout, so those findings fell to the "unknown severity" note and a Major or
  Critical never blocked a merge. Both layouts now read their level on inline
  findings, and the finding's title is reported instead of the raw header.
  Findings CodeRabbit places in its review body (outside the diff) are a
  separate reader and are not covered here.
