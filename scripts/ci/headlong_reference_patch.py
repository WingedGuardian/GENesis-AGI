"""Checked adaptation of the pinned prompt primitive; never modifies stock H."""

import sys
from pathlib import Path

RENDER_RECORD = r'''if $clean == $record and (($line | reference_counts) == ($line | literal_counts))
                   then $line else ($clean | tojson) end'''

GUARD = r'''
def reference_conflict: "reference conflict\n" | halt_error(65);
def redact_card($pattern):
    . as $text |
    [$references[] as $ref | match($ref; "g") | {start: .offset, end: (.offset + .length)}] as $spans |
    reduce match($pattern; "g") as $m ({text: "", cursor: 0};
        ($m.captures | map(select(.name == "edge"))[0]) as $edge |
        ($m.captures | map(select(.name == "end"))[0]) as $end |
        ($m.offset + $edge.length) as $start |
        ($m.offset + $m.length - $end.length) as $stop |
        ($spans | any(.start <= $start and .end >= $stop)) as $inside |
        ($spans | any(.start < $stop and .end > $start)) as $overlap |
        if $overlap and ($inside | not) then reference_conflict else
            .text += $text[.cursor:$m.offset] +
                (if $inside then $m.string else $edge.string + "<redacted:card>" + $end.string end) |
            .cursor = ($m.offset + $m.length)
        end) | .text + $text[.cursor:];
def semantic_strings:
    if type == "string" then .
    elif type == "array" then .[] | semantic_strings
    elif type == "object" then to_entries[] | .key, (.value | semantic_strings)
    else empty end;
def reference_counts:
    [split("\n")[] | . as $line | (try fromjson catch null) as $record |
        if ($record | type) == "object" or ($record | type) == "array"
        then $record | semantic_strings else $line end] as $strings |
    [$references[] as $ref | [$strings[] | match($ref; "g")] | length];
def literal_counts:
    . as $text | [$references[] as $ref | [$text | match($ref; "g")] | length];
'''


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("pinned reference adaptation does not match exactly once")
    return source.replace(before, after)


def adapt_common(source):
    start = source.index("prompt_redact() (\n")
    prefix, body = source[:start], source[start:]
    body = replace_once(body, '''    printf '%s' "$1" | jq -Rsrj '\n''',
                        '''    local references
    references=$(python3 /fixture/headlong_prompt_references.py) || return $?
    printf '%s' "$1" | jq --argjson references "$references" -Rsrj '\n''' + GUARD)
    for pattern in (r"(?<edge>^|[^0-9])[0-9]{4}([ -][0-9]{4}){2,3}(?<end>[^0-9]|$)",
                    r"(?<edge>^|[^0-9A-Za-z])[0-9]{15,16}(?<end>[^0-9]|$)"):
        body = replace_once(body, 'gsub("' + pattern + r'"; "\(.edge)<redacted:card>\(.end)")',
                            'redact_card("' + pattern + '")')
    body = replace_once(body, r'''        split("\n") | map(. as $line''',
                        r'''        reference_counts as $before |
        split("\n") | map(. as $line''')
    body = replace_once(body, r'if $clean == $record then $line else ($clean | tojson) end', RENDER_RECORD)
    body = replace_once(body, "        | redact_string\n    '\n)",
                        "        | redact_string\n        | if reference_counts == $before then . else reference_conflict end\n    '\n)")
    return prefix + body


if __name__ == "__main__":
    path = Path(sys.argv[1]) / "thinkers/_lib/common.sh"
    path.write_text(adapt_common(path.read_text()))
