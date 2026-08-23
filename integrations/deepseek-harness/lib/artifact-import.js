const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
export function artifactImageInput(content) {
    const mediaType = content.artifact.content_type;
    if (!mediaType || !IMAGE_TYPES.has(mediaType)) {
        throw new Error(`Harness Attachment import supports images only; Artifact is ${mediaType || 'unknown'}`);
    }
    if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(content.content)) {
        throw new Error('Orbit Artifact content is not canonical base64');
    }
    const data = Uint8Array.from(Buffer.from(content.content, 'base64'));
    return {
        data, mediaType: mediaType,
        name: String(content.artifact.name || content.artifact.filename || content.artifact.artifact_id),
    };
}
