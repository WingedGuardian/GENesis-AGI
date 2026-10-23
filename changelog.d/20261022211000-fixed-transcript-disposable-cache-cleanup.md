Recover malformed disposable analytics cache entries during rebuilding, preserve
symlink targets, and sync the parent directory after discarding a restored cache,
including retries after a directory-sync failure has already removed the cache.
