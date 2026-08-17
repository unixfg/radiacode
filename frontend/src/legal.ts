const DEFAULT_SOURCE_REPOSITORY = "https://github.com/unixfg/radiacode";
const FULL_GIT_SHA = /^[0-9a-f]{40}$/i;

function repositoryUrl(configured: string | undefined): string {
  const candidate = configured?.trim().replace(/\/+$/, "") || DEFAULT_SOURCE_REPOSITORY;
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol === "https:" || parsed.protocol === "http:") return candidate;
  } catch {
    // Fall through to the known public repository.
  }
  return DEFAULT_SOURCE_REPOSITORY;
}

export function sourceUrl(
  configuredRepository: string | undefined = import.meta.env.VITE_SOURCE_URL,
  configuredRevision: string | undefined = import.meta.env.VITE_SOURCE_REVISION,
): string {
  const repository = repositoryUrl(configuredRepository);
  const revision = configuredRevision?.trim();
  return revision && FULL_GIT_SHA.test(revision)
    ? `${repository}/tree/${revision.toLowerCase()}`
    : repository;
}

export const THIRD_PARTY_NOTICES_URL = "/assets/THIRD_PARTY_NOTICES.md";
