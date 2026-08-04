import math


def distance_2d(p1, p2):
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def move_points_opposite(p1, p2, new_dist, angle_deg):
    """Translate p1 and p2 parallel to the line of direction angle_deg by
    equal and opposite amounts, until their separation equals new_dist.
    Midpoint and the perpendicular offset between p1 and p2 are preserved."""
    a = math.radians(angle_deg)
    ux, uy = math.cos(a), math.sin(a)

    vx, vy = p2[0] - p1[0], p2[1] - p1[1]
    v_par = vx * ux + vy * uy  # component of (p2-p1) along u
    v_perp_sq = (vx * vx + vy * vy) - v_par * v_par

    # After moving, parallel component becomes v_par + 2t; perpendicular is unchanged.
    # Need (v_par + 2t)^2 + v_perp^2 = new_dist^2.
    rhs = new_dist * new_dist - v_perp_sq
    if rhs < 0:
        raise ValueError(
            f"new_dist={new_dist} is smaller than the perpendicular offset "
            f"({math.sqrt(v_perp_sq):.4f}) between p1 and p2 relative to the "
            "angle line; translation parallel to that line cannot reach it."
        )

    # Keep sign of parallel component (so p1 and p2 don't swap along u).
    sign = 1.0 if v_par >= 0 else -1.0
    t = (sign * math.sqrt(rhs) - v_par) / 2

    return (p1[0] - t * ux, p1[1] - t * uy), (p2[0] + t * ux, p2[1] + t * uy)


# example
p1 = (104.2827, 157.32)
p2 = (251.3173, 167.48)
# new_dist = 144.196
new_dist = 144.546
angle_deg = 0


print("Original:")
print("p1 =", p1)
print("p2 =", p2)
print(f"Current distance = {distance_2d(p1, p2):.3f}")


p1_new, p2_new = move_points_opposite(p1, p2, new_dist, angle_deg)

print("\nMoved:")
print(f"p1 = {p1_new[0]:.4f}, {p1_new[1]:.4f}")
print(f"p2 = {p2_new[0]:.4f}, {p2_new[1]:.4f}")
print(f"New distance = {distance_2d(p1_new, p2_new):.3f}")
