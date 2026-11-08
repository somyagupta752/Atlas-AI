// Role helpers. The BACKEND enforces every one of these rules; the UI uses them only to show the right controls and to
// explain why a control is unavailable. Hiding a button is a courtesy, not a protection.
export const RANK = { viewer: 1, editor: 2, owner: 3, admin: 3 };

export function can(role, minimum) {
  return (RANK[role] || 0) >= RANK[minimum];
}

export const NEEDS = {
  index: "editor",
  search: "viewer",
  members: "owner",
};

export function whyNot(role, minimum) {
  return can(role, minimum) ? null : `This needs the ${minimum} role. You are a ${role || "non-member"}.`;
}
