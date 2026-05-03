import { useNavigate } from 'react-router-dom'
import { faceThumbnailUrl } from '../../api/media'
import type { FaceOnMedia } from '../../api/types'

interface FaceStripProps {
  faces: FaceOnMedia[]
}

export function FaceStrip({ faces }: FaceStripProps) {
  const navigate = useNavigate()
  const labeled = Object.values(
    faces
      .filter(f => f.person_id)
      .reduce<Record<string, FaceOnMedia>>((acc, f) => {
        const key = f.person_id!
        if (!acc[key] || f.confidence > acc[key].confidence) acc[key] = f
        return acc
      }, {})
  )

  if (labeled.length === 0) return null

  return (
    <div className="flex gap-1 flex-wrap">
      {labeled.map(face => (
        <button
          key={face.face_id}
          title={face.person_name ?? undefined}
          onClick={e => {
            e.stopPropagation()
            navigate(`/people?person=${face.person_id}`)
          }}
          className="group flex flex-col items-center"
        >
          <img
            src={faceThumbnailUrl(face.face_id)}
            alt={face.person_name ?? 'face'}
            className="w-7 h-7 rounded-full object-cover ring-1 ring-zinc-600 group-hover:ring-indigo-400 transition"
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
        </button>
      ))}
    </div>
  )
}
