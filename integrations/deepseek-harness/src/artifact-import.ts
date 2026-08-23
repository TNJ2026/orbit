import type { ImageMediaType, SaveImageAttachment } from '@deepseek-ai/dsh-attachment'
import type { ArtifactContent } from './types.js'

const IMAGE_TYPES = new Set<ImageMediaType>(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])

export function artifactImageInput(content: ArtifactContent): SaveImageAttachment {
  const mediaType = content.artifact.content_type
  if (!mediaType || !IMAGE_TYPES.has(mediaType as ImageMediaType)) {
    throw new Error(`Harness Attachment import supports images only; Artifact is ${mediaType || 'unknown'}`)
  }
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(content.content)) {
    throw new Error('Orbit Artifact content is not canonical base64')
  }
  const data = Uint8Array.from(Buffer.from(content.content, 'base64'))
  return {
    data, mediaType: mediaType as ImageMediaType,
    name: String(content.artifact.name || content.artifact.filename || content.artifact.artifact_id),
  }
}
