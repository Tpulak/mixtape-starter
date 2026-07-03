# Mixtape Bug Hunt — Submission

## AI Usage

I used Cursor's AI assistant throughout this project in several ways:

- **Codebase orientation:** I asked the AI to read `README.md`, `models.py`, all five service files, and the route layer together, then summarize responsibilities and trace call chains (e.g., rating a song from `POST /songs/<id>/rate` through `notification_service.rate_song()`).
- **Bug investigation:** After reading the code myself, I used the AI to confirm my hypotheses — for example, explaining that Python's `datetime.weekday()` returns `6` for Sunday, and that an `outerjoin` on `song_tags` multiplies SQL rows for multi-tag songs.
- **Verification:** The AI's initial suggestion for Issue #3 assumed duplicates would always appear in ORM `.all()` results. I verified with a raw SQL query that the join produces 3 rows for a 3-tag song, confirming the join was the root cause even though SQLAlchemy's identity map sometimes hides duplicates in Python. I kept the `.distinct()` fix rather than trusting the "no bug" observation from a single test run.
- **Documentation:** The AI helped draft this submission doc and root cause analyses, which I reviewed against the actual code changes before committing.

Where the AI was most helpful was explaining unfamiliar SQLAlchemy join behavior and datetime conventions. Where I had to override it was assuming all five bugs would fail existing tests — Issues #2 and #4 had no pre-existing tests, and Issue #3's test passed on SQLite due to ORM deduplication despite incorrect SQL.

---

## Codebase Map

*(Written before any bug fixes were applied.)*

### Main Files and Roles

| File | Role |
|------|------|
| `app.py` | Flask application factory. Configures SQLite DB, initializes SQLAlchemy, registers four blueprints under `/songs`, `/playlists`, `/users`, and `/feed`. |
| `models.py` | SQLAlchemy models: `User`, `Song`, `Tag`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`. Association tables `friendships`, `song_tags`, and `playlist_entries` (with `position` column for ordered playlist songs). |
| `routes/songs.py` | HTTP endpoints for search, song detail, rating (`POST /rate`), and listening events (`POST /listen`). Delegates to `search_service`, `notification_service`, and `streak_service`. |
| `routes/playlists.py` | Playlist CRUD and adding songs. `POST .../songs` calls `notification_service.add_to_playlist()`. |
| `routes/users.py` | User profile, streak lookup, and notification retrieval/mark-read. |
| `routes/feed.py` | Friends listening now and activity feed endpoints. |
| `services/streak_service.py` | Records listening events and updates consecutive-day streaks on `User`. |
| `services/feed_service.py` | Builds "Friends Listening Now" (recency-filtered) and activity feed (unfiltered). |
| `services/search_service.py` | Case-insensitive title/artist search. |
| `services/notification_service.py` | Creates notifications for playlist adds and song ratings; retrieves/marks read. |
| `services/playlist_service.py` | Creates playlists and returns ordered song lists via `playlist_entries.position`. |
| `seed_data.py` | Populates 5 users, 13 songs (varying tag counts), 3 playlists, listening events, and sample notifications. |

### Pattern

Every route is a thin wrapper: parse/validate input → call one service function → format JSON response. All business logic lives in `services/`.

### Data Flow — Friend Adds Your Song to a Playlist

1. Client sends `POST /playlists/<playlist_id>/songs` with `{song_id, added_by}`.
2. `routes/playlists.py` calls `notification_service.add_to_playlist()`.
3. `add_to_playlist()` loads the song, adder, and playlist; appends the song to `playlist.songs` if not already present; commits.
4. If `song.shared_by != added_by_user_id`, it calls `create_notification()` with type `song_added_to_playlist` and a message naming the adder, song title, and playlist name.
5. `create_notification()` inserts a `Notification` row and commits.
6. The sharer sees it via `GET /users/<user_id>/notifications`.

---

## Root Cause Analyses

### Issue #1 — My listening streak keeps resetting

**How you reproduced it**

Ran `pytest tests/test_streaks.py::test_streak_increments_on_sunday -v`. The test simulates listening on Saturday (2024-06-15) then Sunday (2024-06-16) with `days_since_last == 1`. Expected streak of 2; got 1 (reset instead of increment). This only fails on Sunday — weekday tests pass.

**How you found the root cause**

Started in `services/streak_service.py` → `update_listening_streak()`. Traced the branch logic for `days_since_last == 1`. Noticed the extra condition `today.weekday() != 6` on line 73. Confirmed with Python that Sunday's `weekday()` is `6`, so the increment branch is skipped on Sunday even when the user listened the previous day.

**The root cause**

In `update_listening_streak()`, consecutive-day increment requires `days_since_last == 1 and today.weekday() != 6`. Python's `weekday()` returns `6` for Sunday. When a user listens Saturday and again Sunday (`days_since_last == 1`), the Sunday check fails, execution falls through to the `else` branch, and the streak resets to 1 instead of incrementing. The condition appears intended to handle a "week boundary" but incorrectly treats Sunday as a non-consecutive day.

**Your fix and side-effect check**

Removed `and today.weekday() != 6` so any `days_since_last == 1` increments the streak regardless of day of week. Ran full `tests/test_streaks.py` — all five tests pass, including Monday→Tuesday increment, same-day no double-count, skip-day reset, and Saturday→Sunday increment.

---

### Issue #2 — Friends Listening Now shows people from yesterday

**How you reproduced it**

After seeding (`python seed_data.py`), inspected `seed_data.py`: recent events are within 30 minutes; older events are 2–58 hours ago. Called `get_friends_listening_now()` for user `nova` (who has friends with both recent and hours-old events). With `RECENT_THRESHOLD = timedelta(hours=24)`, friends who listened 2–18 hours ago appeared in the feed alongside those who listened 10 minutes ago — stale "yesterday" activity mixed with truly current listeners.

**How you found the root cause**

Traced `GET /feed/<user_id>/listening-now` → `feed_service.get_friends_listening_now()`. Found `RECENT_THRESHOLD = timedelta(hours=24)` and filter `listened_at >= cutoff`. The seed data comment explicitly says recent events are "within the past 30 minutes" and older events "should NOT appear in listening now after fix." The 24-hour window is far too wide for a "listening **now**" feature.

**The root cause**

`RECENT_THRESHOLD` was set to 24 hours, so any friend who listened within the past day appeared in "Friends Listening Now." Events from many hours ago (effectively yesterday evening) passed the cutoff and were shown as if currently listening.

**Your fix and side-effect check**

Changed `RECENT_THRESHOLD` to `timedelta(minutes=30)` to match the seed data's definition of "recent." Verified that only events within the last 30 minutes appear. Confirmed `get_activity_feed()` is unchanged — it still returns historical events without recency filtering, so the general activity feed is unaffected.

---

### Issue #3 — The same song keeps showing up twice in search

**How you reproduced it**

Ran a raw SQL query against the search join:

```sql
SELECT song.id FROM song
LEFT OUTER JOIN song_tags ON song.id = song_tags.song_id
WHERE song.title LIKE '%Crown%'
```

For "Crown Heights Anthem" (3 tags), this returned **3 rows** for one song. Searching `GET /songs/search?q=Crown` could return duplicate entries depending on database/ORM behavior. The existing pytest for multi-tag songs documents the expected behavior (exactly 1 result).

**How you found the root cause**

Read `search_service.search_songs()`. The query `outerjoin`s `song_tags` even though the filter only checks `title` and `artist` — tags are loaded separately via the `Song.tags` relationship in `to_dict()`. The unnecessary join multiplies rows: a song with N tags produces N result rows at the SQL level. This is conditional — songs with 0 or 1 tag don't duplicate; songs with 2+ tags do.

**The root cause**

The `outerjoin(song_tags, ...)` creates one SQL row per tag association. A song with three tags appears three times in the raw result set. Without `.distinct()`, duplicate songs leak into search results (and the `count` field) for multi-tag songs.

**Your fix and side-effect check**

Added `.distinct()` before `.all()` on the query. All five tests in `tests/test_search.py` pass. Tags still appear correctly in each song dict via the ORM relationship — the join was never needed for tag data.

---

### Issue #4 — Not notified when a friend rated my song

**How you reproduced it**

Compared `notification_service.add_to_playlist()` (working) with `rate_song()` (broken) line by line. `add_to_playlist()` calls `create_notification()` when someone else adds your song. `rate_song()` saves the rating and commits but never calls `create_notification()`. Seeded app has a sample `song_added_to_playlist` notification in `seed_data.py` demonstrating the expected pattern; no equivalent exists for ratings.

**How you found the root cause**

Traced `POST /songs/<song_id>/rate` → `routes/songs.py` → `notification_service.rate_song()`. Read the full function — it handles validation, upsert of `Rating`, and commit, then returns. No notification logic anywhere. The architectural pattern used elsewhere (check sharer ≠ actor, then `create_notification()`) was simply missing from the rating path.

**The root cause**

`rate_song()` persisted the rating but did not notify the song's original sharer. Unlike `add_to_playlist()`, which checks `song.shared_by != added_by_user_id` and calls `create_notification()`, the rating function had no equivalent notification step after commit.

**Your fix and side-effect check**

After committing the rating, added the same guard and a `create_notification()` call with type `song_rated` and a message including the rater's username, song title, and score. Self-ratings (`user_id == song.shared_by`) do not notify. Added regression tests in `tests/test_notifications.py` (`test_rate_song_notifies_sharer`, `test_rate_own_song_does_not_notify`).

---

### Issue #5 — The last song in a playlist never shows up

**How you reproduced it**

Ran `pytest tests/test_playlists.py::test_playlist_returns_all_songs -v`. Playlist seeded with 5 songs; `get_playlist_songs()` returned 4. `test_playlist_returns_songs_in_order` confirmed "Track 5" was missing from the end of the list.

**How you found the root cause**

Traced `GET /playlists/<id>/songs` → `playlist_service.get_playlist_songs()`. The SQL query correctly fetches all songs ordered by `position`. The return statement on line 66 was `[song.to_dict() for song in songs[:-1]]` — Python slice `[:-1]` excludes the last element.

**The root cause**

An off-by-one error in the list comprehension: `songs[:-1]` deliberately drops the final song in the playlist. The query returned the correct data; the bug was purely in the return statement slicing away the last item.

**Your fix and side-effect check**

Changed return to `[song.to_dict() for song in songs]` (no slice). All three playlist tests pass, including empty playlist (returns `[]`) and order verification for all 5 tracks.

---

## Regression Test

Added `tests/test_notifications.py` for Issue #4. These tests would have caught the missing notification logic before merge:

- `test_rate_song_notifies_sharer` — friend rates your song → `song_rated` notification created
- `test_rate_own_song_does_not_notify` — self-rating → no notification

Existing tests in `tests/test_streaks.py`, `tests/test_search.py`, and `tests/test_playlists.py` serve as regression tests for Issues #1, #3, and #5 respectively.

---

## Git Log

Run `git log --oneline` on branch `bugfix/mixtape`:

```
48c3538 fix: return all playlist songs instead of excluding the last one
4e6420f fix: notify song sharer when a friend rates their song
e56c5ae fix: deduplicate search results when songs have multiple tags
477639f fix: narrow listening-now window from 24 hours to 30 minutes
c7a3715 fix: remove incorrect Sunday check that reset streak on consecutive days
```

*(Screenshot of this output should be attached in the Course Portal submission.)*
