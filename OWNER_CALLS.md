# Owner calls first — execute these BEFORE any bulk commits.
# Run each line separately and confirm; do not fold into `git add -A`.

# 1. Drop WhatsApp session secrets from tracking (files stay on disk).
#    The companion key `XKIRO_API_KEY_2` was verified live in config + env.
git rm --cached -q wa-bridge/wa-bridge/auth/creds.json wa-bridge/wa-bridge/auth/session-*.json wa-bridge/wa-bridge/auth/sender-key-*.json

# 2. Scoped .gitignore additions for the auth path (run after the rm).
#    (Applied via editor tool; shown here for the record.)
#    wa-bridge/wa-bridge/auth/

# 3. Optionally delete personal root files (stays on disk unless you say so).
#    rm _toolnames.txt chaining_out.txt chaining_err.txt jarvis_upgrade_recommendations.md stark_surprise_for_shyam.txt
