import { ReplitConnectors } from "@replit/connectors-sdk";

const USER_FIELDS = [
  "verified",
  "verified_type",
  "public_metrics",
  "description",
  "created_at",
].join(",");
const TWEET_FIELDS = [
  "created_at",
  "public_metrics",
  "lang",
  "author_id",
  "referenced_tweets",
].join(",");

type XProfile = {
  id: string;
  name?: string;
  username?: string;
  verified?: boolean;
  verified_type?: string;
  public_metrics?: { followers_count?: number };
};

type XPost = {
  id: string;
  text?: string;
  created_at?: string;
  author_id?: string;
  public_metrics?: Record<string, number>;
  referenced_tweets?: Array<{ type?: string; id?: string }>;
};

async function xGet(path: string): Promise<{ data?: unknown[]; includes?: { users?: XProfile[] }; errors?: unknown[] }> {
  const connectors = new ReplitConnectors();
  const response = await connectors.proxy("x", path, { method: "GET" });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`X API ${response.status}: ${detail.slice(0, 300)}`);
  }
  return (await response.json()) as {
    data?: unknown[];
    includes?: { users?: XProfile[] };
    errors?: unknown[];
  };
}

export async function fetchXSnapshot(
  usernames: string[],
  lookbackSeconds: number,
  maxPosts: number,
) {
  const cleanUsernames = [...new Set(
    usernames
      .map((value) => value.trim().replace(/^@/, "").toLowerCase())
      .filter((value) => /^[a-z0-9_]{1,15}$/.test(value)),
  )].slice(0, 10);

  if (cleanUsernames.length === 0) {
    return { fetched_at: new Date().toISOString(), profiles: [], posts: [] };
  }

  const profilePath = `/2/users/by?usernames=${encodeURIComponent(cleanUsernames.join(","))}&user.fields=${encodeURIComponent(USER_FIELDS)}`;
  const profileResponse = await xGet(profilePath);
  const profiles = (profileResponse.data ?? []) as XProfile[];
  const profileByUsername = new Map(
    profiles
      .filter((profile) => profile.username)
      .map((profile) => [profile.username!.toLowerCase(), profile]),
  );

  const since = new Date(Date.now() - lookbackSeconds * 1000).toISOString();
  const query = `(${cleanUsernames.map((username) => `from:${username}`).join(" OR ")}) -is:retweet`;
  const postPath = `/2/tweets/search/recent?query=${encodeURIComponent(query)}&start_time=${encodeURIComponent(since)}&max_results=${Math.min(100, Math.max(10, maxPosts))}&tweet.fields=${encodeURIComponent(TWEET_FIELDS)}&expansions=author_id&user.fields=${encodeURIComponent(USER_FIELDS)}`;
  const postResponse = await xGet(postPath);
  const includedProfiles = postResponse.includes?.users ?? [];
  const mergedProfiles = [...profiles, ...includedProfiles];
  const uniqueProfiles = [...new Map(
    mergedProfiles
      .filter((profile) => profile.id)
      .map((profile) => [profile.id, profile]),
  ).values()];

  return {
    fetched_at: new Date().toISOString(),
    profiles: uniqueProfiles,
    posts: (postResponse.data ?? []) as XPost[],
    requested_usernames: cleanUsernames,
    profile_errors: profileResponse.errors ?? [],
    post_errors: postResponse.errors ?? [],
    profile_by_username: Object.fromEntries(profileByUsername),
  };
}