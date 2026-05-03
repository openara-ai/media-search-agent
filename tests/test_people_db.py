import sqlite3
from pathlib import Path
from msa_indexer.db.sqlite_store import SQLiteStore

def init_db(tmp_path: Path) -> SQLiteStore:
    db_path = tmp_path / "test.db"
    store = SQLiteStore(db_path)
    schema_path = Path(__file__).parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"
    store.init_schema(schema_path)
    return store

def test_people_crud_and_labeling(tmp_path):
    db = init_db(tmp_path)

    # Create two people
    p1 = db.create_person("Alice")
    p2 = db.create_person("Bob")

    people = db.list_people()
    names = sorted([p["name"] for p in people])
    assert names == ["Alice", "Bob"]

    # Insert a dummy media and face to label
    db.conn.execute(
        """
        INSERT INTO media(media_id, path, mime) VALUES(?, ?, ?)
        """,
        ("m1", "/tmp/photo.jpg", "image/jpeg"),
    )
    db.conn.execute(
        """
        INSERT INTO face(face_id, media_id, x, y, w, h, confidence)
        VALUES(?, ?, 0.1, 0.1, 0.2, 0.2, 0.9)
        """,
        ("f1", "m1"),
    )
    db.commit()

    # Label face with Alice
    db.update_face_person("f1", p1["person_id"])
    faces = db.get_media_faces("m1")
    assert len(faces) == 1
    assert faces[0]["person_id"] == p1["person_id"]

    # Rename Alice to Alicia
    db.rename_person(p1["person_id"], "Alicia")
    person = db.get_person(p1["person_id"])
    assert person and person["name"] == "Alicia"

    # Merge Bob into Alicia
    reassigned = db.merge_people(p2["person_id"], p1["person_id"])
    assert isinstance(reassigned, int)

    # Unassign face
    db.clear_face_person("f1")
    faces = db.get_media_faces("m1")
    assert faces[0]["person_id"] is None

    db.close()
