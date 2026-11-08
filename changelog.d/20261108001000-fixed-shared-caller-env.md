In shared mode, the explicit caller environment accessor now returns the
untrusted lost-scope caller even after environment drift. Validated request
identity and ordinary legacy environment behavior retain their existing paths.
