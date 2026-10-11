- NVIDIA health probes now use its models catalog instead of incorrectly reporting
a missing API key when the probe URL was absent. Catalog observations cannot
change NVIDIA breakers. Unsupported probe targets retain breaker-based display;
the health API distinguishes probe support, credential presence and breaker
authority. Model routing and qualification are unchanged.
