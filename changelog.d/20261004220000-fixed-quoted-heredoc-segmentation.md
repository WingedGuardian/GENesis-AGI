- A here-document under a quoted delimiter (`<<'EOF'`) no longer leaks its body
  into the command segmenter: prose and code-shaped text in a literal body is
  data, not executable segments. Bodies fed to a resolved executor
  (`bash <<'EOF'`, `cat <<'EOF' | bash`, …) still scan as programs — a
  suppression that hid those would have been a gate bypass.
