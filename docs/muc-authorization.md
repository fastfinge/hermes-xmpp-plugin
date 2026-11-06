# MUC sender authorization

Room membership is not permission to invoke the bot. A group message must pass
both checks:

- The room is present in `muc_rooms`.
- The sender's verified personal bare JID is present in `muc_allowed_users`.

The sender list accepts a YAML list or a comma-separated string.
`XMPP_MUC_ALLOWED_USERS` is the profile-scoped environment alternative. If unset,
the list defaults to DM `allowed_users`. An explicit empty list denies everyone.
`allow_all_users` bypasses DM checks only; it never bypasses MUC authorization.

```yaml
xmpp:
  muc_rooms: "room@conference.example.org/bot"
  muc_allowed_users: ["owner@example.org", "tester@example.org"]
```

## Verified identities

Identity comes from Slixmpp's XEP-0045 server-presence cache for the exact room
and occupant. A message-supplied JID, display name, or nickname cannot authorize
its sender. Missing identity denies invocation before OMEMO decryption, forms,
attachment downloads, or gateway dispatch. The authenticated room server remains
the authority for these identities.

Semi-anonymous rooms normally expose real JIDs to moderators. Arrange the bot's
room permissions separately, usually through admin/owner affiliation. Moderator
is a role, not an affiliation. Fully anonymous rooms cannot satisfy this policy.
The plugin does not modify server or room configuration. Successful verified
MUC admission is also passed through Hermes' adapter-grant mechanism, so a
MUC-only allowlist entry survives the core authorization gate without gaining
DM access.

## Migration from room-only authorization

Previously, adding a room could authorize every occupant. Existing deployments
must now allow the intended personal JIDs and ensure the bot receives their
server-reported identities. If those conditions are not met, the bot stays silent
in that room. This applies to text, voice, and clarification submissions alike.

Tests cover authorized, unauthorized, and unresolved senders, forged message
identities, separate room/sender lists, and denial before side effects. Live
room acceptance remains necessary because server privacy settings differ.
