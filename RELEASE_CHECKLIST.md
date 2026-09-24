# GitHub Release Checklist

- [ ] Copy `.env.example` to `.env` locally and fill in secrets; never commit `.env`.
- [ ] Confirm no tokens, API keys, personal IDs, logs, audio files, or generated media are tracked.
- [ ] Review `README.md` and confirm the responsible-use and consent requirements are accurate.
- [ ] Run `python -m py_compile voice_bot.py`.
- [ ] Run `git diff --check`.
- [ ] Test `!join`, `!leave`, speech recognition, Gemini replies, and TTS in an authorized server.
- [ ] Verify `DM_LOGS_ENABLED=false` unless private DM logging is explicitly required.
- [ ] Verify `.env.example` contains placeholders only.
- [ ] Review the final diff, commit, and push to GitHub.
- [ ] Create a GitHub release only after the first clean run from a fresh checkout.
