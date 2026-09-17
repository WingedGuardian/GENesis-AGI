- **Qdrant's memory-protection setting is now one the system will actually
  honour.** The vector store that Genesis depends on declared a protection level
  no user-level service is permitted to set, so the setting was refused — quietly,
  with no error anywhere, while every place you could look to check it kept
  reporting the value it never got. Existing installs are migrated in place, since
  this particular service has no other path to receive the fix, and it is
  restarted so the change actually takes effect rather than merely being written
  down.
- **The check that watches for this class of problem now looks at every service
  Genesis installs**, rather than only those whose names begin with "genesis".
  Two of its own services fell outside that, including the one this change fixes —
  so the check would have stayed silent about exactly the thing it exists to
  catch. A test now derives the list from what the repo actually installs, so a
  newly added service cannot quietly fall outside it again.
