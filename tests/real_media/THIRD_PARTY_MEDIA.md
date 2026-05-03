# Third-Party Test Media

These files are included as public test fixtures for automated validation of:

- face detection
- object detection
- metadata extraction
- scene detection
- keyframe extraction
- end-to-end indexing and API checks

Preserve this file alongside the fixture media.

## Images

### face_single_01.jpg
Source: https://commons.wikimedia.org/wiki/File:Girl_close-up_face_portrait_(49749906893).jpg
Author: Pedro Ribeiro Simoes
License: CC BY 2.0
License URL: https://creativecommons.org/licenses/by/2.0/
Notes: Single-face portrait. Commons page includes a personality-rights warning.
Changes: Copied into repo fixture set; EXIF-edited derived copies may be created for testing.

### exif_gps_face_01.heic
Source: Derived from `face_single_01.jpg`
Author: Derived artifact based on Pedro Ribeiro Simoes source image above
License: CC BY 2.0
License URL: https://creativecommons.org/licenses/by/2.0/
Notes: Public HEIC fixture used for HEIC decoding plus GPS/camera metadata extraction tests.
Changes: Converted from `face_single_01.jpg` with ImageMagick HEIC encoding; added controlled GPS/camera/lens metadata with `exiftool`.

### face_single_02.jpg
Source: https://commons.wikimedia.org/wiki/File:Close_up_portrait_(8354439191).jpg
Author: David Rosen
License: CC BY 2.0
License URL: https://creativecommons.org/licenses/by/2.0/
Notes: Single-face portrait.
Changes: Copied into repo fixture set; EXIF-edited derived copies may be created for testing.

### face_expressive_01.jpg
Source: https://commons.wikimedia.org/wiki/File:Very_Expressive_Face_(6182711795).jpg
Author: Tony Alter
License: CC BY 2.0
License URL: https://creativecommons.org/licenses/by/2.0/
Notes: Single-face portrait with a different expression.
Changes: Copied into repo fixture set; EXIF-edited derived copies may be created for testing.

### face_group_01.jpg
Source: https://commons.wikimedia.org/wiki/File:Participants_group_photo_2.0.jpg
Author: Wilhelmmarvel
License: CC0 1.0
License URL: https://creativecommons.org/publicdomain/zero/1.0/
Notes: Multi-person group photo.
Changes: Copied into repo fixture set; EXIF-edited derived copies may be created for testing.

### face_same_person_01.jpg
Source: https://commons.wikimedia.org/wiki/File:Face_portrait_(50242061457).jpg
Author: Sabine Mondestin
License: CC BY 2.0
License URL: https://creativecommons.org/licenses/by/2.0/
Notes: Same-subject portrait of Sabine Mondestin used for deterministic face-labeling and similar-face tests.
Changes: Copied into repo fixture set; EXIF-edited derived copies may be created for testing.

### face_same_person_02.jpg
Source: https://commons.wikimedia.org/wiki/File:Face_portrait_(50241202183).jpg
Author: Sabine Mondestin
License: CC BY 2.0
License URL: https://creativecommons.org/licenses/by/2.0/
Notes: Second same-subject portrait of Sabine Mondestin used for deterministic face-labeling and similar-face tests.
Changes: Copied into repo fixture set; EXIF-edited derived copies may be created for testing.

### object_dog_01.jpg
Source: https://commons.wikimedia.org/wiki/File:Photo_of_a_dog.jpg
Author: ContaDeletada2906
License: CC0 1.0
License URL: https://creativecommons.org/publicdomain/zero/1.0/
Notes: Dog/object detection fixture.
Changes: Copied into repo fixture set; EXIF-edited derived copies may be created for testing.

### object_landscape_01.jpg
Source: https://commons.wikimedia.org/wiki/File:Volcanic_Landscape_(223180293).jpg
Author: Tony Hisgett
License: CC BY 2.0
License URL: https://creativecommons.org/licenses/by/2.0/
Notes: No-face / low-object control image.
Changes: Copied into repo fixture set; EXIF-edited derived copies may be created for testing.

## Videos

### video_face_single_01.webm
Source: https://commons.wikimedia.org/wiki/File:Wikimedia.webm
Author: OJjnr
License: CC0 1.0
License URL: https://creativecommons.org/publicdomain/zero/1.0/
Notes: Very short talking-head clip; useful for minimal face-in-video smoke tests.
Changes: Copied into repo fixture set; shorter derived clips may be created for testing.

### video_people_crowd_01.webm
Source: https://commons.wikimedia.org/wiki/File:Bradford_Town_Hall_Square_(1896).webm
Author: Unknown filmmaker; imported to Commons by Yann from YouTube
License: Public domain
License URL: https://commons.wikimedia.org/wiki/File:Bradford_Town_Hall_Square_(1896).webm
Notes: Crowd/street motion; useful for scene/keyframe extraction and coarse person detection.
Changes: Copied into repo fixture set; shorter derived clips may be created for testing.

### video_street_objects_01.webm
Source: https://commons.wikimedia.org/wiki/File:Street_traffic.webm
Author: Editor
License: CC BY 3.0
License URL: https://creativecommons.org/licenses/by/3.0/
Notes: Street scene for person/vehicle object detection and keyframe extraction.
Changes: Copied into repo fixture set; shorter derived clips may be created for testing.

### video_dog_object_01.webm
Source: https://commons.wikimedia.org/wiki/File:Miss_Oreo_(O-Dog).webm
Author: Fancibaer
License: CC0 1.0
License URL: https://creativecommons.org/publicdomain/zero/1.0/
Notes: Short dog clip for non-person object detection.
Changes: Copied into repo fixture set; shorter derived clips may be created for testing.

### video_dog_motion_01.webm
Source: https://commons.wikimedia.org/wiki/File:Summer_Dog_-_video.webm
Author: oakleyoriginals
License: CC BY 2.0
License URL: https://creativecommons.org/licenses/by/2.0/
Notes: Moving dog clip with more variation than a static animal shot.
Changes: Copied into repo fixture set; shorter derived clips may be created for testing.

## Reuse Notes

- `CC0` and public-domain files are the simplest fixtures for redistribution.
- `CC BY` files require attribution, which this file is intended to satisfy.
- Some files depicting identifiable people may still carry personality/privacy considerations separate from copyright.
- Prefer keeping untouched originals plus smaller derived test clips/images.
- Source pages for `video_street_objects_01.webm`, `video_dog_object_01.webm`, `video_dog_motion_01.webm`, and `video_people_crowd_01.webm` were re-checked on April 4, 2026.
