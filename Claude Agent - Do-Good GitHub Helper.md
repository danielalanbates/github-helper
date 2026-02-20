## 2026-02-19 08:45 PM — PR SUBMITTED
**Repo:** actualbudget/actual
**Issue/PR:** #6401 / PR #7035 — Fix category select always choosing first item on touch devices
**Details:** On Android tablets (and potentially other touch devices), tapping a category in the autocomplete dropdown always selected the first item instead of the tapped item. Root cause: Downshift's `onInputValueChange` was not filtering out `clickItem` events, causing intermediate state changes (resetting `highlightedIndex` to 0 and reopening the dropdown) that interfered with the selection on touch devices. Fix adds `clickItem` to the early-return filter list and removes redundant partial guards.
**Action needed:** No — PR submitted at https://github.com/actualbudget/actual/pull/7035
---

## 2026-02-20 14:30 — CONFIG UPDATE
**Summary:** Per-issue opus budget system implemented
**Changes:**
- ✅ Removed MAX_PRS_PER_REPO (no longer needed)
- ✅ Changed MAX_OPUS_PER_REPO → MAX_OPUS_PER_ISSUE (now per-issue instead of per-repo)
- ✅ Added migrations 13-14 for tracking opus_attempts and pr_closed_at
- ✅ Updated model_selector.py to use per-issue opus counting
- ✅ Updated orchestrator.py to track opus attempts per issue
- ✅ Updated solver.py to record model_used and opus_attempts in contributions
- ✅ Quick escalation to OPUS for bounty issues (already working)

**How it works:**
- Each issue gets max 10 opus attempts (instead of 10 per entire repo)
- Sonnet used for initial attempts, escalates to opus on failure (complex cases)
- Bounty issues always get opus (priority flag)
- Contributions table now tracks: model_used, opus_attempts, pr_closed_at, pr_close_reason

**Next steps:**
- Monitor closed PRs: when detected, review feedback and attempt resubmit as new PR
- Abuse watch system: flag editors creating many bounties expecting free opus tokens
---
