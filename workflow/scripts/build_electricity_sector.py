"""Functions for building electricity infrastructure in sector studies"""

import numpy as np
import pypsa
from constants_sector import SecCarriers


def build_electricty(
    n: pypsa.Network,
    sector: str,
) -> None:
    """Adds electricity sector infrastructre data"""

    add_electricity_infrastructure(n, sector)
    add_electricity_stores(n, sector)


def add_electricity_infrastructure(n: pypsa.Network, sector: str):
    """
    Adds links to connect electricity nodes.

    For example, will build the link between "p480 0" and "p480 0 res-
    elec"
    """

    elec = SecCarriers.ELECTRICITY.value

    df = n.loads[n.loads.index.str.endswith(f"{sector}-{elec}")].copy()

    df["bus0"] = df.apply(lambda row: row.bus.split(f" {row.carrier}")[0], axis=1)
    df["bus1"] = df.bus
    df["sector"] = df.carrier.map(lambda x: x.split("-")[0])
    df.index = df["bus0"] + " " + df["sector"]
    df["carrier"] = df["sector"] + f"-{elec}"

    n.madd(
        "Link",
        df.index,
        suffix=f"-{elec}",
        bus0=df.bus0,
        bus1=df.bus1,
        carrier=df.carrier,
        efficiency=1,
        capital_cost=0,
        p_nom_extendable=True,
        lifetime=np.inf,
    )


def add_electricity_stores(
    n: pypsa.Network,
    sector: str,
) -> None:
    """
    Adds stores to the network to use for demand response.
    """

    elec = SecCarriers.ELECTRICITY.value

    df = n.loads[n.loads.index.str.endswith(f"{sector}-{elec}")].copy()
    df["x"] = df.bus.map(n.buses.x)
    df["y"] = df.bus.map(n.buses.y)
    df["carrier"] = df["carrier"]
    df = df.set_index("bus")

    n.madd(
        "Bus",
        df.index,
        suffix=f"-store",
        x=df.x,
        y=df.y,
        carrier=df.carrier,
        unit="MWh",
    )

    # p_nom set to zero
    # demand response config will override this setting

    n.madd(
        "Link",
        df.index,
        suffix=f"-charger",
        bus0=df.index,
        bus1=df.index + "-store",
        efficiency=1,
        carrier=df.carrier,
        p_nom_extendable=False,
        p_nom=0,
    )

    n.madd(
        "Link",
        df.index,
        suffix=f"-discharger",
        bus0=df.index + "-store",
        bus1=df.index,
        efficiency=1,
        carrier=df.carrier,
        p_nom_extendable=False,
        p_nom=0,
    )

    n.madd(
        "Store",
        df.index,
        bus=df.index + "-store",
        e_cyclic=True,
        e_nom_extendable=False,
        e_nom=np.inf,
        carrier=df.carrier,
        standing_loss=0,
        capital_cost=0,
        lifetime=np.inf,
    )
