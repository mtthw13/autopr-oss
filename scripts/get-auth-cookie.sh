#!/usr/bin/env bash
# Get a Supabase auth cookie for Lighthouse testing.
#
# Usage:
#   ./scripts/get-auth-cookie.sh <supabase-url> <anon-key> <email> <password>
#
# Example:
#   ./scripts/get-auth-cookie.sh \
#     https://<project-ref>.supabase.co \
#     <anon-key> \
#     you@example.com \
#     <password>
#
# Output: the cookie string to put in autopr.yaml under research.lighthouse.auth_cookie

set -euo pipefail

SUPABASE_URL="${1:?Usage: $0 <supabase-url> <anon-key> <email> <password>}"
ANON_KEY="${2:?}"
EMAIL="${3:?}"
PASSWORD="${4:?}"

# Sign in via Supabase REST API
RESPONSE=$(curl -s -X POST \
  "${SUPABASE_URL}/auth/v1/token?grant_type=password" \
  -H "apikey: ${ANON_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"${EMAIL}\", \"password\": \"${PASSWORD}\"}")

ACCESS_TOKEN=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)
REFRESH_TOKEN=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['refresh_token'])" 2>/dev/null)

if [ -z "$ACCESS_TOKEN" ]; then
  echo "ERROR: Failed to get access token" >&2
  echo "$RESPONSE" >&2
  exit 1
fi

# Extract project ref from URL (e.g. <project-ref>)
PROJECT_REF=$(echo "$SUPABASE_URL" | sed 's|https://||' | sed 's|\.supabase\.co.*||')

# Supabase SSR stores the token as a chunked cookie
# For Lighthouse --extra-headers, we pass it as a single cookie
COOKIE="sb-${PROJECT_REF}-auth-token=base64-${ACCESS_TOKEN}"

echo "$COOKIE"
echo ""
echo "# Add to autopr.yaml:"
echo "# research:"
echo "#   lighthouse:"
echo "#     auth_cookie: \"${COOKIE}\""
