"""
tests/test_notifications.py — Mixtape

Regression tests for notification creation when friends interact with shared songs.
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed_users_and_song(app):
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Neon City", artist="Night Drive", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rate_song_notifies_sharer(app, seed_users_and_song):
    """Rating a friend's shared song should create a song_rated notification."""
    with app.app_context():
        data = seed_users_and_song
        rate_song(data["rater"].id, data["song"].id, 5)

        notifications = get_notifications(data["sharer"].id)
        rated = [n for n in notifications if n["type"] == "song_rated"]

        assert len(rated) == 1
        assert "Neon City" in rated[0]["body"]
        assert "rater" in rated[0]["body"]


def test_rate_own_song_does_not_notify(app, seed_users_and_song):
    """Rating your own song should not create a notification."""
    with app.app_context():
        data = seed_users_and_song
        rate_song(data["sharer"].id, data["song"].id, 4)

        notifications = get_notifications(data["sharer"].id)
        assert notifications == []
