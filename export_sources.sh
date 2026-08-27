#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

OUTPUT="project_sources.txt"

EXCLUDE_DIRS=(
  "*/venv/*"
  "*/brouter-src/*"
  "*/__pycache__/*"
  "./.git/*"
  "*/dist/*"
  "*/node_modules/*"
)

CODE_EXTENSIONS=(py yaml yml json sh brf nsi plist)
MD_EXTENSIONS=(md)

find_by_extensions() {
  local args=(. "(")
  local first=1
  for dir in "${EXCLUDE_DIRS[@]}"; do
    if [ $first -eq 1 ]; then
      args+=(-path "$dir")
      first=0
    else
      args+=(-o -path "$dir")
    fi
  done
  args+=(")" -prune -o "(")
  first=1
  for ext in "$@"; do
    if [ $first -eq 1 ]; then
      args+=(-name "*.$ext")
      first=0
    else
      args+=(-o -name "*.$ext")
    fi
  done
  args+=(")" -type f -print)
  find "${args[@]}"
}

CODE_FILES=()
while IFS= read -r line; do
  CODE_FILES+=("$line")
done < <(find_by_extensions "${CODE_EXTENSIONS[@]}" | sed 's|^\./||' | sort)

MD_FILES=()
while IFS= read -r line; do
  MD_FILES+=("$line")
done < <(find_by_extensions "${MD_EXTENSIONS[@]}" | sed 's|^\./||' | sort)

ALL_FILES=("${CODE_FILES[@]}" "${MD_FILES[@]}")

{
  echo "================================================================================"
  echo "PROJECT SOURCES EXPORT"
  echo "Generated: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Total files: ${#ALL_FILES[@]}"
  echo "================================================================================"
  echo ""
  echo "FILE LIST:"
  for f in "${ALL_FILES[@]}"; do
    echo "  - $f"
  done
  echo ""
} > "$OUTPUT"

append_file() {
  local f="$1"
  {
    echo "================================================================================"
    echo "FILE: $f"
    echo "================================================================================"
    cat "$f"
    echo ""
    echo ""
  } >> "$OUTPUT"
}

for f in "${CODE_FILES[@]}"; do
  append_file "$f"
done

for f in "${MD_FILES[@]}"; do
  append_file "$f"
done

echo "Wrote ${#ALL_FILES[@]} files to $OUTPUT"
