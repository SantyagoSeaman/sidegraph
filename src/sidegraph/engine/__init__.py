"""Engine seam — the ONLY package where Graphify specifics may live.

The portable core (``sidegraph.schema`` / ``store`` / ``retrieval`` / ``server``) depends
on the :class:`~sidegraph.engine.reader.GraphifyReader` protocol here and nothing else about
the engine. A Graphify release breaks at most this package. Do not add a second engine
adapter — keep the seam clean so one is *possible* later without reshaping the core.
"""
