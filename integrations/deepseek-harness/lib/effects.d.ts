export interface WorkspaceEffects {
    baseRevision?: string;
    finalRevision?: string;
    changedFiles: string[];
    createdFiles: string[];
    deletedFiles: string[];
    generatedArtifacts: string[];
    commandCategories: string[];
    observation: 'git-status' | 'unavailable';
}
interface Entry {
    code: string;
    digest?: string;
}
interface Snapshot {
    revision?: string;
    entries: Map<string, Entry>;
}
export declare function snapshotWorkspace(cwd: string): Promise<Snapshot | null>;
export declare function effectManifest(before: Snapshot | null, after: Snapshot | null): WorkspaceEffects;
export {};
