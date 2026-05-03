# Real Media Fixture Manifest

This manifest defines the intended purpose and minimum expected assertions for each
fixture in `tests/real_media/fixtures/`.

Assertions should use thresholds, not exact counts. Model output can vary slightly
across versions, platforms, and hardware.

## Images

### face_single_01.jpg
Purpose: single-person face detection, metadata extraction
Expected:
- at least 1 face detected
- image metadata is readable

### face_single_02.jpg
Purpose: second single-person face example with different subject
Expected:
- at least 1 face detected
- image metadata is readable

### face_expressive_01.jpg
Purpose: robustness against expression changes
Expected:
- at least 1 face detected
- image metadata is readable

### face_group_01.jpg
Purpose: multi-face detection and people-grouping smoke test
Expected:
- at least 2 faces detected
- image metadata is readable

### face_same_person_01.jpg
Purpose: first same-person portrait for deterministic face-labeling and similarity tests
Expected:
- at least 1 face detected
- image metadata is readable

### face_same_person_02.jpg
Purpose: second same-person portrait for deterministic face-labeling and similarity tests
Expected:
- at least 1 face detected
- image metadata is readable

### object_dog_01.jpg
Purpose: object detection for animal class
Expected:
- at least 1 object detected
- expected labels include `dog` or model-equivalent animal label
- no face requirement

### object_landscape_01.jpg
Purpose: no-face / low-object control
Expected:
- 0 faces is acceptable
- image metadata is readable
- no strict object-detection requirement

## Derived Images

### exif_gps_face_01.jpg
Derived from: face_single_01.jpg
Purpose: validate GPS extraction on a face-bearing image
Expected:
- at least 1 face detected
- GPS latitude/longitude parsed
- `Make` and `Model` non-empty

### exif_gps_face_01.heic
Derived from: face_single_01.jpg
Purpose: validate HEIC decoding plus GPS extraction on a face-bearing image
Expected:
- file is readable as HEIC
- GPS latitude/longitude parsed
- `Make`, `Model`, and `LensModel` present

### exif_camera_face_02.jpg
Derived from: face_single_02.jpg
Purpose: validate camera/lens metadata extraction
Expected:
- at least 1 face detected
- `Make` present
- `Model` present
- `LensModel` present

### exif_face_expressive_01.jpg
Derived from: face_expressive_01.jpg
Purpose: validate expression robustness with controlled camera metadata
Expected:
- at least 1 face detected
- `Make` present
- `Model` present
- `LensModel` present

### exif_face_same_person_01.jpg
Derived from: face_same_person_01.jpg
Purpose: validate first same-person portrait with controlled metadata
Expected:
- at least 1 face detected
- `Make` present
- `Model` present
- `LensModel` present

### exif_face_same_person_02.jpg
Derived from: face_same_person_02.jpg
Purpose: validate second same-person portrait with controlled metadata
Expected:
- at least 1 face detected
- `Make` present
- `Model` present
- `LensModel` present

### exif_group_faces_01.jpg
Derived from: face_group_01.jpg
Purpose: validate multiple-face detection plus controlled metadata
Expected:
- at least 2 faces detected
- `Make` and `Model` present

### exif_object_dog_01.jpg
Derived from: object_dog_01.jpg
Purpose: validate object detection plus controlled EXIF
Expected:
- expected labels include `dog`
- `Make` and `Model` present

### exif_object_landscape_01.jpg
Derived from: object_landscape_01.jpg
Purpose: validate metadata extraction on a non-face image
Expected:
- 0 faces is acceptable
- GPS latitude/longitude parsed
- `Make`, `Model`, and `LensModel` present

## Videos

### video_face_single_01.webm
Purpose: minimal face-in-video smoke test
Expected:
- video metadata is readable
- at least 1 sampled frame or keyframe exists
- at least 1 frame contains a detected face

### video_people_crowd_01.webm
Purpose: crowd motion, scene/keyframe extraction, coarse people detection
Expected:
- video metadata is readable
- at least 2 sampled frames or keyframes exist
- at least 1 frame contains a `person` detection
- 0 recognized faces is acceptable if resolution is too poor

### video_street_objects_01.webm
Purpose: street-scene keyframes plus object detection
Expected:
- video metadata is readable
- at least 2 sampled frames or keyframes exist
- expected labels include `person`
- expected labels include at least one vehicle-class object such as `car`, `bus`, `truck`, or `bicycle`

### video_dog_object_01.webm
Purpose: animal detection in video
Expected:
- video metadata is readable
- at least 1 sampled frame or keyframe exists
- expected labels include `dog`

### video_dog_motion_01.webm
Purpose: animal detection across motion frames
Expected:
- video metadata is readable
- at least 2 sampled frames or keyframes exist
- expected labels include `dog`

## Derived Videos

### trimmed_video_face_single_01.webm
Derived from: video_face_single_01.webm
Purpose: repo-friendly trimmed talking-head clip
Expected:
- at least 1 frame contains a face
- duration remains sufficient for frame sampling

### trimmed_video_street_objects_01.webm
Derived from: video_street_objects_01.webm
Purpose: repo-friendly street-scene clip
Expected:
- at least 2 sampled frames or keyframes exist
- expected labels include `person`
- expected labels include at least one vehicle-class object

### trimmed_video_people_crowd_01.webm
Derived from: video_people_crowd_01.webm
Purpose: shorter crowd-motion clip for scene/keyframe smoke tests
Expected:
- at least 2 sampled frames or keyframes exist
- at least 1 frame contains a `person` detection

### trimmed_video_dog_object_01.webm
Derived from: video_dog_object_01.webm
Purpose: shorter dog clip for non-person object smoke tests
Expected:
- at least 1 sampled frame or keyframe exists
- expected labels include `dog`

### trimmed_video_dog_motion_01.webm
Derived from: video_dog_motion_01.webm
Purpose: shorter motion-heavy dog clip for frame sampling checks
Expected:
- at least 2 sampled frames or keyframes exist
- expected labels include `dog`

### trimmed_gopro_gps_01.mp4
Derived from: first-party GoPro GPS sample recorded for public fixture use
Purpose: validate embedded `gpmd` telemetry detection and timed GPS extraction on a real GoPro-style MP4
Expected:
- video metadata is readable
- embedded telemetry track is present
- GPS samples are extractable from the telemetry track
- serial-number fields are not exposed in the sanitized derived copy

## Suggested Folder Layout

```text
tests/real_media/
  fixtures/
    originals/
      face_single_01.jpg
      face_single_02.jpg
      face_expressive_01.jpg
      face_group_01.jpg
      face_same_person_01.jpg
      face_same_person_02.jpg
      object_dog_01.jpg
      object_landscape_01.jpg
      video_face_single_01.webm
      video_people_crowd_01.webm
      video_street_objects_01.webm
      video_dog_object_01.webm
      video_dog_motion_01.webm
    derived/
      exif_gps_face_01.jpg
      exif_gps_face_01.heic
      exif_camera_face_02.jpg
      exif_face_expressive_01.jpg
      exif_face_same_person_01.jpg
      exif_face_same_person_02.jpg
      exif_group_faces_01.jpg
      exif_object_dog_01.jpg
      exif_object_landscape_01.jpg
      trimmed_video_dog_motion_01.webm
      trimmed_video_dog_object_01.webm
      trimmed_video_face_single_01.webm
      trimmed_video_people_crowd_01.webm
      trimmed_video_street_objects_01.webm
```

## Notes

- Keep originals unchanged after download.
- Apply EXIF edits only to derived image files.
- Prefer trimmed derived video clips for repo size and faster tests.
- Prefer threshold assertions over exact counts.
- Keep `THIRD_PARTY_MEDIA.md` adjacent to the fixtures.
- The `face_same_person_*.jpg` pair exists specifically to support deterministic
  real-data tests for face labeling and similar-face top-k behavior.
