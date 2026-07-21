"""The one layout both the catalog and a run's plan draw from."""

from __future__ import annotations

import unittest

from orbit.workflow.api.graph_layout import graph_layout


def edge(source: str, target: str, **extra) -> dict:
    return {"from": source, "to": target, **extra}


class GraphLayoutTests(unittest.TestCase):
    def positions(self, nodes, edges) -> dict[str, tuple[int, int]]:
        layout = graph_layout(nodes, edges)
        return {
            item["node_id"]: (item["depth"], item["lane"])
            for item in layout["positions"]
        }

    def test_a_chain_becomes_one_node_per_column(self) -> None:
        places = self.positions(
            ["collect", "transform", "publish", "done"],
            [edge("collect", "transform"), edge("transform", "publish"),
             edge("publish", "done")],
        )
        self.assertEqual(
            {"collect": (0, 0), "transform": (1, 0), "publish": (2, 0), "done": (3, 0)},
            places,
        )

    def test_a_node_follows_its_parent_whatever_the_node_order(self) -> None:
        """A successor listed before its own branch still lands to its right."""

        places = self.positions(
            ["doneL", "doneR", "left", "right", "start"],
            [edge("start", "left"), edge("start", "right"),
             edge("left", "doneL"), edge("right", "doneR")],
        )
        self.assertEqual((0, 0), places["start"])
        self.assertEqual({(1, 0), (1, 1)}, {places["left"], places["right"]})
        self.assertEqual({(2, 0), (2, 1)}, {places["doneL"], places["doneR"]})

    def test_a_join_sits_past_its_deepest_parent(self) -> None:
        places = self.positions(
            ["a", "b", "c", "join"],
            [edge("a", "b"), edge("b", "c"), edge("a", "join"), edge("c", "join")],
        )
        self.assertEqual(3, places["join"][0])

    def test_a_back_edge_cannot_push_a_node_rightwards(self) -> None:
        forward = [edge("a", "b"), edge("b", "c")]
        self.assertEqual(
            self.positions(["a", "b", "c"], forward),
            self.positions(["a", "b", "c"], forward + [edge("c", "a", back_edge=True)]),
        )

    def test_the_mode_reports_whether_the_graph_forks(self) -> None:
        chain = graph_layout(["a", "b"], [edge("a", "b")])
        forked = graph_layout(["a", "b", "c"], [edge("a", "b"), edge("a", "c")])
        joined = graph_layout(["a", "b", "c"], [edge("a", "c"), edge("b", "c")])
        self.assertEqual("outline", chain["mode"])
        self.assertEqual("branching", forked["mode"])
        self.assertEqual("branching", joined["mode"])

    def test_every_node_is_placed_even_if_forward_edges_form_a_cycle(self) -> None:
        """A cycle should be impossible here; it must not vanish from the picture."""

        places = self.positions(["a", "b"], [edge("a", "b"), edge("b", "a")])
        self.assertEqual({"a", "b"}, set(places))

    def test_edges_naming_an_unknown_node_are_ignored(self) -> None:
        places = self.positions(["a", "b"], [edge("a", "b"), edge("b", "ghost")])
        self.assertEqual({"a": (0, 0), "b": (1, 0)}, places)


if __name__ == "__main__":
    unittest.main()
