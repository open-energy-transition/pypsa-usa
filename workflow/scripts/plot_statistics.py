"""
Plots static and interactive charts to analyze system results.

**Inputs**

A solved network

**Outputs**

System level charts for:
    - Hourly production
    - Generator costs
    - Generator capacity

    .. image:: _static/plots/production-area.png
        :scale: 33 %

    .. image:: _static/plots/costs-bar.png
        :scale: 33 %

    .. image:: _static/plots/capacity-bar.png
        :scale: 33 %

Emission charts for:
    - Accumulated emissions

    .. image:: _static/plots/emissions-area.png
        :scale: 33 %
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pypsa
import seaborn as sns

logger = logging.getLogger(__name__)
import cartopy.crs as ccrs
import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from _helpers import configure_logging
from add_electricity import sanitize_carriers
from add_extra_components import add_nice_carrier_names
from matplotlib.lines import Line2D
from plot_network_maps import get_color_palette
from summary import (
    get_capital_costs,
    get_demand_timeseries,
    get_energy_timeseries,
    get_fuel_costs,
    get_generator_marginal_costs,
    get_node_emissions_timeseries,
    get_tech_emissions_timeseries,
)

# Global Plotting Settings
TITLE_SIZE = 16


def create_title(title: str, **wildcards) -> str:
    """
    Standardizes wildcard writing in titles.

    Arguments:
        title: str
            Title of chart to plot
        **wildcards
            any wildcards to add to title
    """
    w = []
    for wildcard, value in wildcards.items():
        if wildcard == "interconnect":
            w.append(f"interconnect = {value}")
        elif wildcard == "clusters":
            w.append(f"#clusters = {value}")
        elif wildcard == "ll":
            w.append(f"ll = {value}")
        elif wildcard == "opts":
            w.append(f"opts = {value}")
        elif wildcard == "sector":
            w.append(f"sectors = {value}")
    wildcards_joined = " | ".join(w)
    return f"{title} \n ({wildcards_joined})"


def stacked_bar_horizons(
    stats,
    variable,
    variable_units,
    carriers,
):
    carriers = carriers.set_index("nice_name")
    colors_ = carriers["color"]
    carriers_legend = carriers  # to track which carriers have non-zero values
    # Create subplots
    planning_horizons = stats[list(stats.keys())[0]].columns
    fig, axes = plt.subplots(
        nrows=len(planning_horizons),
        ncols=1,
        figsize=(8, 1.2 * len(planning_horizons)),
        sharex=True,
    )

    # Ensure axes is always iterable (even if there's only one planning horizon)
    if len(planning_horizons) == 1:
        axes = [axes]

    # Loop through each planning horizon
    for ax, horizon in zip(axes, planning_horizons):
        y_positions = np.arange(len(stats))  # One position for each scenario
        for j, (scenario, df) in enumerate(stats.items()):
            bottoms = np.zeros(
                len(df.columns),
            )  # Initialize the bottom positions for stacking
            # Stack the technologies for each scenario
            for i, technology in enumerate(df.index.unique()):
                values = df.loc[technology, horizon]
                values = values / (1e3) if "GW" in variable_units else values
                ax.barh(
                    y_positions[j],
                    values,
                    left=bottoms[j],
                    color=colors_[technology],
                    label=technology if j == 0 else "",
                )
                bottoms[j] += values
                carriers_legend.loc[technology, "value"] = values

        # Set the title for each subplot
        ax.text(
            1.01,
            0.5,
            f"{horizon}",
            transform=ax.transAxes,
            va="center",
            rotation="vertical",
        )
        ax.set_yticks(y_positions)  # Positioning scenarios on the y-axis
        ax.set_yticklabels(stats.keys())  # Labeling y-axis with scenario names
        ax.grid(True, axis="x", linestyle="--", alpha=0.5)

    # Create legend handles and labels from the carriers DataFrame
    carriers_legend = carriers_legend[carriers_legend["value"] > 0.01]
    colors_ = carriers_legend["color"]
    legend_handles = [plt.Rectangle((0, 0), 1, 1, color=colors_[tech]) for tech in carriers_legend.index]
    # fig.legend(handles=legend_handles, labels=carriers.index.tolist(), loc='lower center', bbox_to_anchor=(0.5, -0.4), ncol=4, title='Technologies')
    ax.legend(
        handles=legend_handles,
        labels=carriers_legend.index.tolist(),
        loc="upper center",
        bbox_to_anchor=(0.5, -1.3),
        ncol=4,
        title="Technologies",
    )

    fig.subplots_adjust(hspace=0, bottom=0.5)
    fig.suptitle(f"{variable}", fontsize=12, fontweight="bold")
    plt.xlabel(f"{variable} {variable_units}")
    # fig.tight_layout()
    # plt.show(block=True)
    return fig


#### Bar Plots ####
def plot_capacity_additions_bar(
    n: pypsa.Network,
    carriers_2_plot: list[str],
    save: str,
    **wildcards,
) -> None:
    """
    Plots base capacity vs optimal capacity as a bar chart.
    """

    existing_capacity = n.generators.groupby("carrier").p_nom.sum().round(0)
    existing_capacity = existing_capacity.to_frame(name="Existing Capacity")
    storage_units = n.storage_units.groupby("carrier").p_nom.sum().round(0)
    storage_units = storage_units.to_frame(name="Existing Capacity")
    existing_capacity = pd.concat([existing_capacity, storage_units])
    existing_capacity.index = existing_capacity.index.map(n.carriers.nice_name)

    optimal_capacity = n.statistics.optimal_capacity()
    optimal_capacity = optimal_capacity[optimal_capacity.index.get_level_values(0).isin(["Generator", "StorageUnit"])]
    optimal_capacity.index = optimal_capacity.index.droplevel(0)
    optimal_capacity.reset_index(inplace=True)
    optimal_capacity.rename(columns={"index": "carrier"}, inplace=True)

    optimal_capacity.set_index("carrier", inplace=True)
    optimal_capacity.insert(0, "Existing", existing_capacity["Existing Capacity"])
    optimal_capacity = optimal_capacity.fillna(0)
    # color_palette = get_color_palette(n)
    # color_mapper = [color_palette[carrier] for carrier in optimal_capacity.index]

    stats = {"": optimal_capacity}
    variable = "Optimal Capacity"
    variable_units = " GW"
    fig_ = stacked_bar_horizons(stats, variable, variable_units, n.carriers)
    fig_.savefig(save)
    plt.close()


def plot_production_bar(
    n: pypsa.Network,
    carriers_2_plot: list[str],
    save: str,
    **wildcards,
) -> None:
    """
    Plot diaptch per carrier.
    """
    energy_mix = n.statistics.supply().round(0)
    energy_mix = energy_mix[
        energy_mix.index.get_level_values("component").isin(
            ["Generator", "StorageUnit"],
        )
    ]
    energy_mix.index = energy_mix.index.droplevel(0)
    energy_mix = energy_mix.fillna(0)
    stats = {"": energy_mix}
    variable = "Energy Mix"
    variable_units = " GWh"

    fig_ = stacked_bar_horizons(stats, variable, variable_units, n.carriers)
    fig_.savefig(save)
    plt.close()


def plot_global_constraint_shadow_prices(
    n: pypsa.Network,
    save: str,
    **wildcards,
) -> None:
    """
    Plots shadow prices on global constraints.
    """

    shadow_prices = n.global_constraints.mu.round(3).reset_index()

    # plot data
    fig, ax = plt.subplots(figsize=(10, 10))

    sns.barplot(
        y=shadow_prices.GlobalConstraint,
        x=shadow_prices.mu,
        data=shadow_prices,
        color="purple",
        ax=ax,
    )

    ax.set_title(create_title("Shadow Prices on Constraints", **wildcards))
    ax.set_ylabel("")
    ax.set_xlabel("Shadow Price [$/MWh]")
    fig.tight_layout()
    fig.savefig(save)
    plt.close()


def plot_regional_capacity_additions_bar(
    n: pypsa.Network,
    save: str,
    **wildcards,
) -> None:
    """
    PLOT OF CAPACITY ADDITIONS BY STATE AND CARRIER (STACKED BAR PLOT)
    """
    exp_gens = n.generators.p_nom_opt - n.generators.p_nom
    exp_storage = n.storage_units.p_nom_opt - n.storage_units.p_nom

    expanded_capacity = pd.concat([exp_gens, exp_storage])
    expanded_capacity = expanded_capacity.to_frame(name="mw")
    mapper = pd.concat(
        [
            n.generators.bus.map(n.buses.nerc_reg),
            n.storage_units.bus.map(n.buses.nerc_reg),
        ],
    )
    expanded_capacity["region"] = expanded_capacity.index.map(mapper)
    carrier_mapper = pd.concat([n.generators.carrier, n.storage_units.carrier])
    expanded_capacity["carrier"] = expanded_capacity.index.map(carrier_mapper)

    palette = n.carriers.color.to_dict()

    expanded_capacity["positive"] = expanded_capacity["mw"] > 0
    df_sorted = expanded_capacity.sort_values(by=["region", "carrier"])
    # Correcting the bottoms for positive and negative values
    bottoms_pos = df_sorted[df_sorted["positive"]].groupby("region")["mw"].cumsum() - df_sorted["mw"]
    bottoms_neg = df_sorted[~df_sorted["positive"]].groupby("region")["mw"].cumsum() - df_sorted["mw"]

    # Re-initialize plot to address the legend and gap issues
    fig, ax = plt.subplots(figsize=(10, 6))

    # Plot each carrier, adjusting handling for legend and correcting negative stacking
    for i, carrier in enumerate(df_sorted["carrier"].unique()):
        # Filter by carrier
        df_carrier = df_sorted[df_sorted["carrier"] == carrier]

        # Separate positive and negative
        df_pos = df_carrier[df_carrier["positive"]]
        df_neg = df_carrier[~df_carrier["positive"]]

        # Plot positives
        ax.barh(
            df_pos["region"],
            df_pos["mw"],
            left=bottoms_pos[df_pos.index],
            color=palette[carrier],
            edgecolor=None,
        )

        # Plot negatives
        ax.barh(
            df_neg["region"],
            df_neg["mw"],
            left=bottoms_neg[df_neg.index],
            color=palette[carrier],
            edgecolor=None,
        )

    # Adjust legend to include all carriers
    handles, labels = [], []
    for i, carrier in enumerate(df_sorted["carrier"].unique()):
        handle = plt.Rectangle((0, 0), 1, 1, color=palette[carrier])
        handles.append(handle)
        labels.append(f"{carrier}")

    ax.legend(handles, labels, title="Carrier")

    ax.set_title("Adjusted MW by Region and Carrier with Negative Values")
    ax.set_xlabel("MW")
    ax.set_ylabel("Region")

    fig.tight_layout()
    fig.savefig(save)
    plt.close()


def plot_regional_emissions_bar(
    n: pypsa.Network,
    save: str,
    **wildcards,
) -> None:
    """
    PLOT OF CO2 EMISSIONS BY REGION.
    """
    regional_emisssions = get_node_emissions_timeseries(n).T.groupby(n.buses.country).sum().T.sum() / 1e6

    plt.figure(figsize=(10, 10))
    sns.barplot(
        x=regional_emisssions.values,
        y=regional_emisssions.index,
        palette="viridis",
        hue=regional_emisssions.index,
        legend=False,
    )

    plt.xlabel("CO2 Emissions [MMtCO2]")
    plt.ylabel("")
    plt.title(create_title("CO2 Emissions by Region", **wildcards))

    plt.tight_layout()
    plt.savefig(save)
    plt.close()


#### Temporal Plots ####


def plot_production_area(
    n: pypsa.Network,
    carriers_2_plot: list[str],
    save: str,
    **wildcards,
) -> None:
    """
    Plot timeseries production.

    Will plot an image for the entire time horizon, in addition to
    seperate monthly generation curves
    """

    # get data

    energy_mix = get_energy_timeseries(n).mul(1e-3)  # MW -> GW
    demand = get_demand_timeseries(n).mul(1e-3)  # MW -> GW

    for carrier in energy_mix.columns:
        if "battery" in carrier or carrier in snakemake.params.electricity["extendable_carriers"]["StorageUnit"]:
            energy_mix[carrier + "_discharger"] = energy_mix[carrier].clip(lower=0.0001)
            energy_mix[carrier + "_charger"] = energy_mix[carrier].clip(upper=-0.0001)
            energy_mix = energy_mix.drop(columns=carrier)
            carriers_2_plot.append(f"{carrier}" + "_charger")
            carriers_2_plot.append(f"{carrier}" + "_discharger")
    carriers_2_plot = list(set(carriers_2_plot))
    energy_mix = energy_mix[[x for x in carriers_2_plot if x in energy_mix]]
    energy_mix = energy_mix.rename(columns=n.carriers.nice_name)

    color_palette = get_color_palette(n)

    months = n.snapshots.get_level_values(1).month.unique()
    num_periods = len(n.investment_periods)
    base_plot_size = 4

    for month in ["all"] + months.to_list():
        figsize = (14, (base_plot_size * num_periods))
        fig, axs = plt.subplots(figsize=figsize, ncols=1, nrows=num_periods)
        if not isinstance(axs, np.ndarray):  # only one horizon
            axs = np.array([axs])
        for i, investment_period in enumerate(n.investment_periods):
            if month == "all":
                sns = n.snapshots[n.snapshots.get_level_values(0) == investment_period]
            else:
                sns = n.snapshots[
                    (n.snapshots.get_level_values(0) == investment_period)
                    & (n.snapshots.get_level_values(1).month == month)
                ]
            energy_mix.loc[sns].droplevel("period").round(2).plot.area(
                ax=axs[i],
                alpha=0.7,
                color=color_palette,
            )
            demand.loc[sns].droplevel("period").round(2).plot.line(
                ax=axs[i],
                ls="-",
                color="darkblue",
            )

            suffix = "-" + datetime.strptime(str(month), "%m").strftime("%b") if month != "all" else ""

            axs[i].legend(bbox_to_anchor=(1, 1), loc="upper left")
            # axs[i].set_title(f"Production in {investment_period}")
            axs[i].set_ylabel("Power [GW]")
            axs[i].set_xlabel("")

        fig.tight_layout(rect=[0, 0, 1, 0.92])
        fig.suptitle(create_title("Production [GW]", **wildcards))
        save = Path(save)
        fig.savefig(save.parent / (save.stem + suffix + save.suffix))
        plt.close()


def plot_hourly_emissions(n: pypsa.Network, save: str, **wildcards) -> None:
    """
    Plots snapshot emissions by technology.
    """

    # get data
    emissions = get_tech_emissions_timeseries(n).mul(1e-6)  # T -> MT
    zeros = emissions.columns[(np.abs(emissions) < 1e-7).all()]
    emissions = emissions.drop(columns=zeros)

    # plot
    color_palette = get_color_palette(n)

    fig, ax = plt.subplots(figsize=(14, 4))
    if not emissions.empty:
        emissions.plot.area(
            ax=ax,
            alpha=0.7,
            legend="reverse",
            color=color_palette,
        )

    ax.legend(bbox_to_anchor=(1, 1), loc="upper left")
    ax.set_title(create_title("Technology Emissions", **wildcards))
    ax.set_ylabel("Emissions [MT]")
    fig.tight_layout()

    fig.savefig(save)
    plt.close()


def plot_accumulated_emissions_tech(n: pypsa.Network, save: str, **wildcards) -> None:
    """
    Creates area plot of accumulated emissions by technology.
    """

    # get data

    emissions = get_tech_emissions_timeseries(n).cumsum().mul(1e-6)  # T -> MT
    zeros = emissions.columns[(np.abs(emissions) < 1e-7).all()]
    emissions = emissions.drop(columns=zeros)

    # plot

    color_palette = get_color_palette(n)

    fig, ax = plt.subplots(figsize=(14, 4))
    if not emissions.empty:
        emissions.plot.area(
            ax=ax,
            alpha=0.7,
            legend="reverse",
            color=color_palette,
        )

    ax.legend(bbox_to_anchor=(1, 1), loc="upper left")
    ax.set_title(create_title("Technology Accumulated Emissions", **wildcards))
    ax.set_ylabel("Emissions [MT]")
    fig.tight_layout()

    fig.savefig(save)
    plt.close()


def plot_accumulated_emissions(n: pypsa.Network, save: str, **wildcards) -> None:
    """
    Plots accumulated emissions.
    """

    # get data

    emissions = get_tech_emissions_timeseries(n).mul(1e-6).sum(axis=1)  # T -> MT
    emissions = emissions.cumsum().to_frame("co2")

    # plot

    color_palette = get_color_palette(n)

    fig, ax = plt.subplots(figsize=(14, 4))

    emissions.plot.area(
        ax=ax,
        alpha=0.7,
        legend="reverse",
        color=color_palette,
    )

    ax.legend(bbox_to_anchor=(1, 1), loc="upper left")
    ax.set_title(create_title("Accumulated Emissions", **wildcards))
    ax.set_ylabel("Emissions [MT]")
    fig.tight_layout()
    fig.savefig(save)
    plt.close()


def plot_curtailment_heatmap(n: pypsa.Network, save: str, **wildcards) -> None:
    curtailment = n.statistics.curtailment()
    curtailment = curtailment[curtailment.index.get_level_values(0).isin(["StorageUnit", "Generator"])].droplevel(0)
    curtailment = curtailment[curtailment.sum(1) > 0.001].T
    curtailment.index = pd.to_datetime(curtailment.index).tz_localize("utc").tz_convert("America/Los_Angeles")
    curtailment["month"] = curtailment.index.month
    curtailment["hour"] = curtailment.index.hour
    curtailment_group = curtailment.groupby(["month", "hour"]).mean()

    df_long = pd.melt(
        curtailment_group.reset_index(),
        id_vars=["month", "hour"],
        var_name="carrier",
        value_name="MW",
    )
    df_long

    carriers = df_long["carrier"].unique()
    num_carriers = len(carriers)

    rows = num_carriers // 3 + (num_carriers % 3 > 0)
    cols = min(num_carriers, 3)

    # Plotting with dynamic subplot creation based on the number of groups, with wrapping
    fig, axes = plt.subplots(rows, cols, figsize=(3 * cols, 3 * rows))
    axes = axes.flatten()  # Flatten the axes array for easy iteration

    for i, carrier in enumerate(carriers):
        pivot_table = (
            df_long[df_long.carrier == carrier]
            .pivot(index="month", columns="hour", values="MW")
            .astype(float)
            .fillna(0)
        )

        sns.heatmap(pivot_table, ax=axes[i], cmap="viridis")
        axes[i].set_title(carrier)

    # Hide any unused axes if the number of groups is not a multiple of 3
    for j in range(i + 1, rows * cols):
        axes[j].set_visible(False)

    plt.suptitle(create_title("Heatmap of Curtailment by by Carrier", **wildcards))

    plt.tight_layout()
    plt.savefig(save)
    plt.close()


def plot_capacity_factor_heatmap(n: pypsa.Network, save: str, **wildcards) -> None:
    """
    HEATMAP OF RENEWABLE CAPACITY FACTORS BY CARRIER.
    """
    df_long = n.generators_t.p_max_pu.loc[n.investment_periods[0]].melt(
        var_name="bus",
        value_name="p_max_pu",
        ignore_index=False,
    )
    df_long["region"] = df_long["bus"].map(n.generators.bus.map(n.buses.country))
    df_long["carrier"] = df_long["bus"].map(n.generators.carrier)
    df_long["hour"] = df_long.index.hour
    df_long["month"] = df_long.index.month
    df_long.drop(columns="bus", inplace=True)
    df_long = df_long.drop(columns="region").groupby(["carrier", "month", "hour"]).mean().reset_index()

    unique_groups = df_long["carrier"].unique()
    num_groups = len(unique_groups)

    rows = num_groups // 4 + (num_groups % 4 > 0)
    cols = min(num_groups, 4)

    # Plotting with dynamic subplot creation based on the number of groups, with wrapping
    fig, axes = plt.subplots(rows, cols, figsize=(4 * cols, 4 * rows))
    axes = axes.flatten()  # Flatten the axes array for easy iteration

    for i, carrier in enumerate(unique_groups):
        pivot_table = (
            df_long[df_long.carrier == carrier]
            .pivot(index="month", columns="hour", values="p_max_pu")
            .astype(float)
            .fillna(0)
        )
        sns.heatmap(pivot_table, ax=axes[i], cmap="viridis")
        axes[i].set_title(carrier)

    # Hide any unused axes if the number of groups is not a multiple of 3
    for j in range(i + 1, rows * cols):
        axes[j].set_visible(False)

    plt.suptitle("Heatmap of Renewable Capacity Factors by by Carrier")

    plt.tight_layout()
    plt.savefig(save)
    plt.close()


#### Panel / Mixed Plots ####


def plot_generator_data_panel(
    n: pypsa.Network,
    save: str,
    **wildcards,
):

    df_capex_expand = n.generators.loc[
        n.generators.p_nom_extendable & ~n.generators.index.str.contains("existing"),
        :,
    ]
    df_capex_retire = n.generators.loc[
        n.generators.index.str.contains("existing")
        & ~n.generators.carrier.isin(
            [
                "solar",
                "onwind",
                "offwind",
                "offwind_floating",
                "geothermal",
                "oil",
                "hydro",
                "nuclear",
                "load",
            ],
        ),
        :,
    ]

    df_storage_units = n.storage_units.loc[n.storage_units.p_nom_extendable, :].copy()
    df_storage_units.loc[:, "efficiency"] = df_storage_units.efficiency_dispatch
    df_capex_expand = pd.concat([df_capex_expand, df_storage_units])

    df_efficiency = n.generators.loc[
        ~n.generators.carrier.isin(
            ["solar", "onwind", "offwind", "offwind_floating", "hydro", "load"],
        ),
        :,
    ]
    # Create a figure and subplots with 2 rows and 2 columns
    fig, axes = plt.subplots(3, 2, figsize=(10, 12))

    # Plot on each subplot
    sns.lineplot(
        data=get_generator_marginal_costs(n),
        x="timestep",
        y="Value",
        hue="Carrier",
        ax=axes[0, 0],
    )
    sns.barplot(data=df_capex_expand, x="carrier", y="capital_cost", ax=axes[0, 1])
    sns.boxplot(data=df_efficiency, x="carrier", y="efficiency", ax=axes[1, 0])
    sns.barplot(data=df_capex_retire, x="carrier", y="capital_cost", ax=axes[1, 1])

    # Create line plot of declining capital costs
    sns.lineplot(
        data=df_capex_expand[df_capex_expand.build_year > 0],
        x="build_year",
        y="capital_cost",
        hue="carrier",
        ax=axes[2, 0],
    )

    sns.barplot(
        data=n.generators.groupby("carrier").sum().reset_index(),
        y="p_nom",
        x="carrier",
        ax=axes[2, 1],
    )

    # Set titles for each subplot
    axes[0, 0].set_title("Generator Marginal Costs")
    axes[0, 1].set_title("Extendable Capital Costs")
    axes[1, 0].set_title("Plant Efficiency")
    axes[1, 1].set_title("Fixed O&M Costs of Retiring Units")
    axes[2, 0].set_title("Expansion Capital Costs by Carrier")
    axes[2, 1].set_title("Existing Capacity by Carrier")

    # Set labels for each subplot
    axes[0, 0].set_xlabel("")
    axes[0, 0].set_ylabel("$ / MWh")
    # axes[0, 0].set_ylim(0, 200)
    axes[0, 1].set_xlabel("")
    axes[0, 1].set_ylabel("$ / MW-yr")
    axes[1, 0].set_xlabel("")
    axes[1, 0].set_ylabel("MWh_primary / MWh_elec")
    axes[1, 1].set_xlabel("")
    axes[1, 1].set_ylabel("$ / MW-yr")
    axes[2, 0].set_xlabel("Year")
    axes[2, 0].set_ylabel("$ / MW-yr")
    axes[2, 1].set_xlabel("")
    axes[2, 1].set_ylabel("MW")

    # Rotate x-axis labels for each subplot
    for ax in axes.flat:
        ax.tick_params(axis="x", rotation=35)

    # Lay legend out horizontally
    axes[0, 0].legend(
        loc="upper left",
        bbox_to_anchor=(1, 1),
        ncol=1,
        fontsize="xx-small",
    )
    axes[2, 0].legend(fontsize="xx-small")

    fig.tight_layout()
    fig.savefig(save)
    plt.close()


def plot_region_lmps(
    n: pypsa.Network,
    save: str,
    **wildcards,
) -> None:
    """
    Plots a box plot of LMPs for each region.
    """
    df_lmp = n.buses_t.marginal_price
    df_long = pd.melt(
        df_lmp.reset_index(),
        id_vars=["timestep"],
        var_name="bus",
        value_name="lmp",
    )
    df_long["season"] = df_long["timestep"].dt.quarter
    df_long["hour"] = df_long["timestep"].dt.hour
    df_long.drop(columns="timestep", inplace=True)
    df_long["region"] = df_long.bus.map(n.buses.country)

    plt.figure(figsize=(10, 10))

    sns.boxplot(
        df_long,
        x="lmp",
        y="region",
        width=0.5,
        fliersize=0.5,
        linewidth=1,
    )

    plt.title(create_title("LMPs by Region", **wildcards))
    plt.xlabel("LMP [$/MWh]")
    plt.ylabel("Region")
    plt.tight_layout()
    plt.savefig(save)
    plt.close()


#### Fuel costs


def plot_fuel_costs(
    n: pypsa.Network,
    save: str,
    **wildcards,
) -> None:

    fuel_costs = get_fuel_costs(n)

    fuels = set(fuel_costs.index.get_level_values("carrier"))

    fig, axs = plt.subplots(len(fuels) + 1, 1, figsize=(20, 40))

    color_palette = n.carriers.color.to_dict()

    # plot error plot of all fuels
    df = fuel_costs.droplevel(["bus", "Generator"]).T.resample("d").mean().reset_index().melt(id_vars="timestep")
    sns.lineplot(
        data=df,
        x="timestep",
        y="value",
        hue="carrier",
        ax=axs[0],
        legend=True,
        palette=color_palette,
    )
    axs[0].set_title("Daily Average Fuel Costs [$/MWh]"),
    axs[0].set_xlabel(""),
    axs[0].set_ylabel("$/MWh"),

    # plot bus fuel prices for each fuel
    for i, fuel in enumerate(fuels):
        nice_name = n.carriers.at[fuel, "nice_name"]
        df = fuel_costs.loc[fuel, :, :].droplevel("Generator").T.resample("d").mean().T.groupby(level=0).mean().T
        sns.lineplot(
            data=df,
            legend=False,
            palette="muted",
            dashes=False,
            ax=axs[i + 1],
        )
        axs[i + 1].set_title(f"Daily Average {nice_name} Fuel Costs per Bus [$/MWh]"),
        axs[i + 1].set_xlabel(""),
        axs[i + 1].set_ylabel("$/MWh"),

    fig.savefig(save)
    plt.close()


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake(
            "plot_statistics",
            interconnect="texas",
            clusters=7,
            ll="v1.00",
            opts="REM-400SEG",
            sector="E",
        )
    configure_logging(snakemake)

    # extract shared plotting files
    n = pypsa.Network(snakemake.input.network)
    onshore_regions = gpd.read_file(snakemake.input.regions_onshore)
    retirement_method = snakemake.params.retirement

    sanitize_carriers(n, snakemake.config)

    # mappers
    generating_link_carrier_map = {"fuel cell": "H2", "battery discharger": "battery"}

    # carriers to plot
    carriers = (
        snakemake.params.electricity["conventional_carriers"]
        + snakemake.params.electricity["renewable_carriers"]
        + snakemake.params.electricity["extendable_carriers"]["Generator"]
        + snakemake.params.electricity["extendable_carriers"]["StorageUnit"]
        + snakemake.params.electricity["extendable_carriers"]["Store"]
        + snakemake.params.electricity["extendable_carriers"]["Link"]
        + ["battery_charger", "battery_discharger"]
    )
    carriers = list(set(carriers))  # remove any duplicates

    # plotting theme
    # sns.set_theme("paper", style="darkgrid")
    n.statistics().round(2).to_csv(snakemake.output.statistics)
    n.generators.to_csv(snakemake.output.statistics[:-15] + "/generators.csv")
    # Bar Plots
    plot_capacity_additions_bar(
        n,
        carriers,
        snakemake.output["capacity_additions_bar.pdf"],
        **snakemake.wildcards,
    )
    plot_production_bar(
        n,
        carriers,
        snakemake.output["production_bar.pdf"],
        **snakemake.wildcards,
    )
    plot_global_constraint_shadow_prices(
        n,
        snakemake.output["global_constraint_shadow_prices.pdf"],
        **snakemake.wildcards,
    )
    plot_regional_capacity_additions_bar(
        n,
        snakemake.output["bar_regional_capacity_additions.pdf"],
        **snakemake.wildcards,
    )
    plot_regional_emissions_bar(
        n,
        snakemake.output["bar_regional_emissions.pdf"],
        **snakemake.wildcards,
    )

    # Time Series Plots
    plot_production_area(
        n,
        carriers,
        snakemake.output["production_area.pdf"],
        **snakemake.wildcards,
    )
    plot_hourly_emissions(
        n,
        snakemake.output["emissions_area.pdf"],
        **snakemake.wildcards,
    )
    plot_accumulated_emissions_tech(
        n,
        snakemake.output["emissions_accumulated_tech.pdf"],
        **snakemake.wildcards,
    )
    plot_accumulated_emissions(
        n,
        snakemake.output["emissions_accumulated.pdf"],
        **snakemake.wildcards,
    )
    # plot_curtailment_heatmap(
    #     n,
    #     snakemake.output["curtailment_heatmap.pdf"],
    #     **snakemake.wildcards,
    # )
    # plot_capacity_factor_heatmap(
    #     n,
    #     snakemake.output["capfac_heatmap.pdf"],
    #     **snakemake.wildcards,
    # )
    plot_fuel_costs(
        n,
        snakemake.output["fuel_costs.pdf"],
        **snakemake.wildcards,
    )

    # Panel Plots
    plot_generator_data_panel(
        n,
        snakemake.output["generator_data_panel.pdf"],
        **snakemake.wildcards,
    )

    # Box Plot
    plot_region_lmps(
        n,
        snakemake.output["region_lmps.pdf"],
        **snakemake.wildcards,
    )
