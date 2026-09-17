- Discord sends and polls now go to the channel you actually asked for. `outreach_send`
  accepts a sub-channel name (`channel="announcements"`) instead of only
  `channel="discord"`, which always resolved to one configured default — so an
  announcement could land in the dev channel and still report success. A channel with no
  webhook configured is now refused, by both the message and the poll paths, with an
  error naming the setting to add rather than silently posting somewhere else. The
  refusal is treated as permanent: it is reported once instead of being retried for
  hours, and the reason is carried through to the log rather than dropped.
