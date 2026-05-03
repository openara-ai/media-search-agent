# People

Browse your library by who's in the frame. The indexer detects faces,
groups visually-similar ones, and lets you label clusters with names so
people become a first-class search concept.

## What gets detected

The indexer runs facenet-pytorch (MTCNN detector + InceptionResnetV1
embeddings, MIT-licensed) on every image and every video keyframe. For
each face it stores:

- a face crop (a small thumbnail, used for the People page UI),
- a face embedding (a 512-dim vector used for clustering and similarity),
- the bounding box and confidence score,
- a link back to the source photo or video keyframe.

All of this is local. Face crops, embeddings, and labels never leave your
machine.

## The People page

The **People** page in the navigation has three modes:

| Mode | What you see | When to use it |
|---|---|---|
| **Overview** | A grid of every detected face cluster (labeled and unlabeled), shown as portrait thumbnails | Browsing — quickly see who's in your library |
| **Browse** | Every photo and video moment for a single person | Open a person to see their full timeline |
| **Similar** | Faces visually closest to a chosen face | Faster labeling — find more shots of the same person |

The **size selector** (S / M / L / XL) on the page resizes thumbnails on
the fly so you can shrink everything to skim quickly or blow it up to
double-check a face.

## Labeling

Labeling a face attaches a person name to its cluster. Once a face is
labeled, the person becomes searchable and filterable.

**Single label**

1. Click a face thumbnail to open it.
2. Type a name (or pick an existing person from autocomplete).
3. Save.

**Bulk assign**

The fastest way to onboard a new person:

1. Click an unlabeled face you want to label.
2. Click **Find similar** — the page switches to **Similar** mode and
   shows the closest faces in your library.
3. Multi-select the ones that are the same person.
4. Apply the label to all of them in one action.

This is roughly 10× faster than labeling one face at a time, and you can
repeat the find-similar step until the candidates stop being the right
person.

**Renaming and merging**

- **Rename** a person by editing their name on the People page; the
  change applies everywhere they appear.
- **Merge** duplicate clusters (the same person split into two labels)
  by re-labeling one cluster with the other's name — the embeddings
  collapse into a single person record.

## What labels unlock

After you've labeled people:

- **Filter by person** in the search bar — `birthday cake` + person `Lily`
  returns only photos where Lily appears.
- **Browse a person's timeline** by opening their cluster from the
  People page Overview.
- **Spot people in videos** — labels apply to faces in video keyframes
  too, so a labeled person's appearances in clips show up alongside
  photos.

Labeling is incremental. You can label one person today, three more next
weekend, and the rest never — search and browse continue to work for
unlabeled clusters; they just appear under the auto-generated cluster
name instead of a person name.

## What it's not great at

- **Babies and very young kids.** Face embeddings change a lot in the
  first few years; the same child at 6 months and 3 years often ends up
  in two clusters. You can merge them by labeling both with the same
  name.
- **Heavy occlusion.** Sunglasses, masks, and dramatic lighting reduce
  recall — some shots will end up unclustered or in a wrong cluster.
- **Side profiles and small faces.** The detector skips faces below
  `face_min_size` (default 20 px on the long side). Group photos taken
  from far away may miss the back row.

For tuning the detector — confidence threshold, minimum face size — see
[CONFIGURATION.md](../CONFIGURATION.md#face-recognition).

## Privacy

Faces, embeddings, and labels stay on your machine. The only network
calls related to face recognition are the one-time download of the
facenet-pytorch model weights on first run (see the
[FAQ](../FAQ.md#privacy)).
