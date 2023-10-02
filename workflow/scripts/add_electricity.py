# PyPSA USA Authors
"""
Adds electrical generators and existing hydro storage units to a base network.

Relevant Settings
-----------------

.. code:: yaml

    costs:
        year:
        version:
        dicountrate:
        emission_prices:

    electricity:
        max_hours:
        marginal_cost:
        capital_cost:
        conventional_carriers:
        co2limit:
        extendable_carriers:


    load:
        scaling_factor:

    renewable:
        hydro:
            carriers:
            hydro_max_hours:
            hydro_capital_cost:

    lines:
        length_factor:

.. seealso::
    Documentation of the configuration file ``config/config.yaml`` at :ref:`costs_cf`,
    :ref:`electricity_cf`, :ref:`load_cf`, :ref:`renewable_cf`, :ref:`lines_cf`

Inputs
------


Outputs
-------


Description
-----------



"""

import logging
from itertools import product
import numpy as np
import pandas as pd
import os
import pypsa
from scipy import sparse
import xarray as xr
from _helpers import configure_logging, update_p_nom_max, export_network_for_gis_mapping
import constants
from typing import Dict
from shapely.prepared import prep

idx = pd.IndexSlice

logger = logging.getLogger(__name__)

# can we get rid of this function and use add_mising_carriers instead?
def _add_missing_carriers_from_costs(n, costs, carriers):
    missing_carriers = pd.Index(carriers).difference(n.carriers.index)
    if missing_carriers.empty: return

    emissions_cols = costs.columns.to_series()\
                           .loc[lambda s: s.str.endswith('_emissions')].values
    suptechs = missing_carriers.str.split('-').str[0]
    emissions = costs.loc[suptechs, emissions_cols].fillna(0.)
    emissions.index = missing_carriers
    n.import_components_from_dataframe(emissions, 'Carrier')


def sanitize_carriers(n, config):
    """
    Sanitize the carrier information in a PyPSA Network object.

    The function ensures that all unique carrier names are present in the network's
    carriers attribute, and adds nice names and colors for each carrier according
    to the provided configuration dictionary.

    Parameters
    ----------
    n : pypsa.Network
        A PyPSA Network object that represents an electrical power system.
    config : dict
        A dictionary containing configuration information, specifically the
        "plotting" key with "nice_names" and "tech_colors" keys for carriers.

    Returns
    -------
    None
        The function modifies the 'n' PyPSA Network object in-place, updating the
        carriers attribute with nice names and colors.

    Warnings
    --------
    Raises a warning if any carrier's "tech_colors" are not defined in the config dictionary.
    """

    for c in n.iterate_components():
        if "carrier" in c.df:
            add_missing_carriers(n, c.df.carrier)


    carrier_i = n.carriers.index
    nice_names = (
        pd.Series(config["plotting"]["nice_names"])
        .reindex(carrier_i)
        .fillna(carrier_i.to_series().str.title())
    )
    n.carriers["nice_name"] = n.carriers.nice_name.where(
        n.carriers.nice_name != "", nice_names
    )
    colors = pd.Series(config["plotting"]["tech_colors"]).reindex(carrier_i)
    if colors.isna().any():
        missing_i = list(colors.index[colors.isna()])
        logger.warning(f"tech_colors for carriers {missing_i} not defined in config.")
    n.carriers["color"] = n.carriers.color.where(n.carriers.color != "", colors)


def add_co2_emissions(n, costs, carriers):
    """
    Add CO2 emissions to the network's carriers attribute.
    """
    suptechs = n.carriers.loc[carriers].index.str.split("-").str[0]
    n.carriers.loc[carriers, "co2_emissions"] = costs.co2_emissions[suptechs].values


def load_costs(tech_costs, config, max_hours, Nyears=1.0):
    # set all asset costs and other parameters
    costs = pd.read_csv(tech_costs, index_col=[0, 1]).sort_index()

    # correct units to MW
    costs.loc[costs.unit.str.contains("/kW"), "value"] *= 1e3
    costs.unit = costs.unit.str.replace("/kW", "/MW")

    fill_values = config["fill_values"]
    costs = costs.value.unstack().fillna(fill_values)

    costs["capital_cost"] = (
        (
            calculate_annuity(costs["lifetime"], costs["discount rate"])
            + costs["FOM"] / 100.0
        )
        * costs["investment"]
        * Nyears
    )

    costs.at["OCGT", "fuel"] = costs.at["gas", "fuel"]
    costs.at["CCGT", "fuel"] = costs.at["gas", "fuel"]

    costs["marginal_cost"] = costs["VOM"] + costs["fuel"] / costs["efficiency"]

    costs = costs.rename(columns={"CO2 intensity": "co2_emissions"})

    costs.at["OCGT", "co2_emissions"] = costs.at["gas", "co2_emissions"]
    costs.at["CCGT", "co2_emissions"] = costs.at["gas", "co2_emissions"]

    costs.at["solar", "capital_cost"] = (
        config["rooftop_share"] * costs.at["solar-rooftop", "capital_cost"]
        + (1 - config["rooftop_share"]) * costs.at["solar-utility", "capital_cost"]
    )

    def costs_for_storage(store, link1, link2=None, max_hours=1.0):
        capital_cost = link1["capital_cost"] + max_hours * store["capital_cost"]
        if link2 is not None:
            capital_cost += link2["capital_cost"]
        return pd.Series(
            dict(capital_cost=capital_cost, marginal_cost=0.0, co2_emissions=0.0)
        )

    costs.loc["battery"] = costs_for_storage(
        costs.loc["battery storage"],
        costs.loc["battery inverter"],
        max_hours=max_hours["battery"],
    )
    costs.loc["H2"] = costs_for_storage(
        costs.loc["hydrogen storage underground"],
        costs.loc["fuel cell"],
        costs.loc["electrolysis"],
        max_hours=max_hours["H2"],
    )

    for attr in ("marginal_cost", "capital_cost"):
        overwrites = config.get(attr)
        if overwrites is not None:
            overwrites = pd.Series(overwrites)
            costs.loc[overwrites.index, attr] = overwrites

    return costs


def attach_hydro(n, costs, ppl, profile_hydro, hydro_capacities, carriers, **params):
    add_missing_carriers(n, carriers)
    add_co2_emissions(n, costs, carriers)

    ppl = (
        ppl.query('carrier == "hydro"')
        .reset_index(drop=True)
        .rename(index=lambda s: str(s) + " hydro")
    )
    ror = ppl.query('technology == "Run-Of-River"')
    phs = ppl.query('technology == "Pumped Storage"')
    hydro = ppl.query('technology == "Reservoir"')

    country = ppl["bus"].map(n.buses.country).rename("country")

    inflow_idx = ror.index.union(hydro.index)
    if not inflow_idx.empty:
        dist_key = ppl.loc[inflow_idx, "p_nom"].groupby(country).transform(normed)

        with xr.open_dataarray(profile_hydro) as inflow:
            inflow_countries = pd.Index(country[inflow_idx])
            missing_c = inflow_countries.unique().difference(
                inflow.indexes["countries"]
            )
            assert missing_c.empty, (
                f"'{profile_hydro}' is missing "
                f"inflow time-series for at least one country: {', '.join(missing_c)}"
            )

            inflow_t = (
                inflow.sel(countries=inflow_countries)
                .rename({"countries": "name"})
                .assign_coords(name=inflow_idx)
                .transpose("time", "name")
                .to_pandas()
                .multiply(dist_key, axis=1)
            )

    if "ror" in carriers and not ror.empty:
        n.madd(
            "Generator",
            ror.index,
            carrier="ror",
            bus=ror["bus"],
            p_nom=ror["p_nom"],
            efficiency=costs.at["ror", "efficiency"],
            capital_cost=costs.at["ror", "capital_cost"],
            weight=ror["p_nom"],
            p_max_pu=(
                inflow_t[ror.index]
                .divide(ror["p_nom"], axis=1)
                .where(lambda df: df <= 1.0, other=1.0)
            ),
        )

    if "PHS" in carriers and not phs.empty:
        # fill missing max hours to params value and
        # assume no natural inflow due to lack of data
        max_hours = params.get("PHS_max_hours", 6)
        phs = phs.replace({"max_hours": {0: max_hours}})
        n.madd(
            "StorageUnit",
            phs.index,
            carrier="PHS",
            bus=phs["bus"],
            p_nom=phs["p_nom"],
            capital_cost=costs.at["PHS", "capital_cost"],
            max_hours=phs["max_hours"],
            efficiency_store=np.sqrt(costs.at["PHS", "efficiency"]),
            efficiency_dispatch=np.sqrt(costs.at["PHS", "efficiency"]),
            cyclic_state_of_charge=True,
        )

    if "hydro" in carriers and not hydro.empty:
        hydro_max_hours = params.get("hydro_max_hours")

        assert hydro_max_hours is not None, "No path for hydro capacities given."

        hydro_stats = pd.read_csv(
            hydro_capacities, comment="#", na_values="-", index_col=0
        )
        e_target = hydro_stats["E_store[TWh]"].clip(lower=0.2) * 1e6
        e_installed = hydro.eval("p_nom * max_hours").groupby(hydro.country).sum()
        e_missing = e_target - e_installed
        missing_mh_i = hydro.query("max_hours.isnull()").index

        if hydro_max_hours == "energy_capacity_totals_by_country":
            # watch out some p_nom values like IE's are totally underrepresented
            max_hours_country = (
                e_missing / hydro.loc[missing_mh_i].groupby("country").p_nom.sum()
            )

        elif hydro_max_hours == "estimate_by_large_installations":
            max_hours_country = (
                hydro_stats["E_store[TWh]"] * 1e3 / hydro_stats["p_nom_discharge[GW]"]
            )

        max_hours_country.clip(0, inplace=True)

        missing_countries = pd.Index(hydro["country"].unique()).difference(
            max_hours_country.dropna().index
        )
        if not missing_countries.empty:
            logger.warning(
                "Assuming max_hours=6 for hydro reservoirs in the countries: {}".format(
                    ", ".join(missing_countries)
                )
            )
        hydro_max_hours = hydro.max_hours.where(
            hydro.max_hours > 0, hydro.country.map(max_hours_country)
        ).fillna(6)

        n.madd(
            "StorageUnit",
            hydro.index,
            carrier="hydro",
            bus=hydro["bus"],
            p_nom=hydro["p_nom"],
            max_hours=hydro_max_hours,
            capital_cost=costs.at["hydro", "capital_cost"],
            marginal_cost=costs.at["hydro", "marginal_cost"],
            p_max_pu=1.0,  # dispatch
            p_min_pu=0.0,  # store
            efficiency_dispatch=costs.at["hydro", "efficiency"],
            efficiency_store=0.0,
            cyclic_state_of_charge=True,
            inflow=inflow_t.loc[:, hydro.index],
        )


def attach_breakthrough_renewable_capacities_to_atlite(n, all_be_plants, renewable_carriers):
    plants = pd.read_csv(all_be_plants, dtype={"bus_id": str}, index_col=0).query(
        "bus_id in @n.buses.index"
    )
    plants.replace(["wind_offshore"], ["offwind"], inplace=True)

    for tech in renewable_carriers:
        tech_plants = plants.query("type == @tech")
        tech_plants.index = tech_plants.index.astype(str)

        network_gens = n.generators[n.generators.carrier == tech] #BUG: there are only 2 wind gens in the network and 1 offwind gen.
        # network_buses = n.buses.loc[network_gens.bus.unique()]
        # gens_per_bus = network_gens.groupby("bus").p_nom.count()

        caps = tech_plants.groupby("bus_id").sum().Pmax #namplate capacity per bus
        # caps = caps / gens_per_bus.reindex(caps.index, fill_value=1) ##REVIEW do i need this
        #TODO: #16 Gens excluded from atlite profiles bc of landuse/etc will not be able to be attached if in the breakthrough network

        if caps[~caps.index.isin(network_gens.bus)].sum() > 0:
            missing_capacity = caps[~caps.index.isin(network_gens.bus)].sum()
            logger.info(f"There are {np.round(missing_capacity,1)} MW of {tech} plants that are not in the network. See git issue #16.")

        n.generators.p_nom.update(network_gens.bus.map(caps).dropna())
        n.generators.p_nom_min.update(network_gens.bus.map(caps).dropna())
        logger.info(f"Adding {len(tech_plants)} {tech} generator capacities to the network.")


def attach_breakthrough_renewable_plants(
    n, fn_plants, renewable_carriers, extendable_carriers, costs):

    _add_missing_carriers_from_costs(n, costs, renewable_carriers)

    plants = pd.read_csv(fn_plants, dtype={"bus_id": str}, index_col=0).query(
        "bus_id in @n.buses.index"
    )
    plants.replace(["wind_offshore"], ["offwind"], inplace=True)

    for tech in renewable_carriers:
        tech_plants = plants.query("type == @tech")
        tech_plants.index = tech_plants.index.astype(str)

        logger.info(f"Adding {len(tech_plants)} {tech} generators to the network.")

        if tech in ["wind", "offwind"]: 
            p = pd.read_csv(snakemake.input["wind"], index_col=0)
        else:
            p = pd.read_csv(snakemake.input[tech], index_col=0)
        intersection = set(p.columns).intersection(tech_plants.index) #filters by plants ID for the plants of type tech
        p = p[list(intersection)]


        Nhours = len(n.snapshots)
        p = p.iloc[:Nhours,:]        #hotfix to fit 2016 renewable data to load data

        p.index = n.snapshots
        p.columns = p.columns.astype(str)

        if (tech_plants.Pmax == 0).any():
            # p_nom is the maximum of {Pmax, dispatch}
            p_nom = pd.concat([p.max(axis=0), tech_plants["Pmax"]], axis=1).max(axis=1)
            p_max_pu = (p[p_nom.index] / p_nom).fillna(0)  # some values remain 0
        else:
            p_nom = tech_plants.Pmax
            p_max_pu = p[tech_plants.index] / p_nom

        n.madd(
            "Generator",
            tech_plants.index,
            bus=tech_plants.bus_id,
            p_nom_min=p_nom,
            p_nom=p_nom,
            marginal_cost=tech_plants.GenIOB * tech_plants.GenFuelCost, #(MMBTu/MW) * (USD/MMBTu) = USD/MW
            # marginal_cost_quadratic = tech_plants.GenIOC * tech_plants.GenFuelCost, 
            capital_cost=costs.at[tech, "capital_cost"],
            p_max_pu=p_max_pu, #timeseries of max power output pu
            p_nom_extendable= tech in extendable_carriers["Generator"],
            carrier=tech,
            weight=1.0,
            efficiency=costs.at[tech, "efficiency"],
        )

    # hack to remove generators without capacity (required for SEG to work)
    # shouldn't exist, in fact...

    p_max_pu_norm = n.generators_t.p_max_pu.max()
    remove_g = p_max_pu_norm[p_max_pu_norm == 0.0].index
    logger.info(
        f"removing {len(remove_g)} {tech} generators {remove_g} with no renewable potential."
    )
    n.mremove("Generator", remove_g)

    return n


def add_nice_carrier_names(n, config):
    carrier_i = n.carriers.index
    nice_names = (pd.Series(config['plotting']['nice_names'])
                  .reindex(carrier_i).fillna(carrier_i.to_series().str.title()))
    n.carriers['nice_name'] = nice_names
    colors = pd.Series(config['plotting']['tech_colors']).reindex(carrier_i)
    if colors.isna().any():
        missing_i = list(colors.index[colors.isna()])
        logger.warning(f'tech_colors for carriers {missing_i} not defined in config.')
    n.carriers['color'] = colors


def assign_ads_missing_lat_lon(plants,n):
    import random
    plants_unmatched = plants[plants.latitude.isna() | plants.longitude.isna()]
    plants_unmatched = plants_unmatched[~plants_unmatched.balancing_area.isna()]
    logger.info(f'Assigning lat and lon to {len(plants_unmatched)} plants missing locations.')

    ba_list_map = {'CISC': 'CISO-SCE', 'CISD': 'CISO-SDGE','VEA': 'CISO-VEA','AZPS':'Arizona','SRP':'Arizona','PAID':'PACW','PAUT':'PACW','PAWY':'PACW','IPFE':'IPCO','IPMV':'IPCO','IPTV':'IPCO','TPWR':'BPAT','SCL':'BPAT','CIPV':'CISO-PGAE','CIPB':'CISO-PGAE','SPPC':'CISO-PGAE','TH_PV':'Arizona'}

    plants_unmatched['balancing_area'] = plants_unmatched['balancing_area'].replace(ba_list_map)
    buses = n.buses.copy()

    #assign lat and lon to the plants_unmatched by choosing the bus within the same balancing_area that has the highest v_nom value.
    #Currently randomly assigned to the top 4 buses in the balancing area by v_nom.
    for i, row in plants_unmatched.iterrows():
        # print(row.balancing_area)
        buses_in_area = buses[buses.balancing_area == row.balancing_area].sort_values(by='v_nom', ascending=False)
        top_5_buses = buses_in_area.iloc[:4]
        bus = top_5_buses.iloc[random.randint(0, 3)]
        plants_unmatched.loc[i,'longitude'] = bus.x
        plants_unmatched.loc[i,'latitude'] = bus.y

    plants.loc[plants_unmatched.index] = plants_unmatched
    logger.info(f'{len(plants[plants.latitude.isna() | plants.longitude.isna()])} plants still missing locations.')
    plants = plants.dropna(subset=['latitude','longitude']) #drop any plants that still don't have lat/lon

    return plants


def attach_ads_batteries(n, plants_df,extendable_carriers, costs):
    plants = plants_df.query(
        "bus_assignment in @n.buses.index"
    )

    plants_filt = plants.query("carrier == 'battery' ")
    plants_filt.index = plants_filt.ads_name.astype(str)

    logger.info(f"Adding {len(plants_filt)} Batteries as Stores to the network.")
    logger.info(f"Note: ADS Public data does not include energy capacity(mwhr) for each BESS plant. Capacity is set in config file by key: 'max_hours'.")

    n.madd(
        "StorageUnit",
        plants_filt.index,
        carrier=plants_filt.carrier,
        bus=plants_filt.bus_assignment,
        p_nom=plants_filt['maxcap(mw)'],
        p_nom_extendable='battery' in extendable_carriers['Store'],
        max_hours = snakemake.config['electricity']['max_hours'],
        efficiency_store= 1.0,
        efficiency_dispatch=1.0, #TODO: Add efficiency_dispatch to config file
        cyclic_state_of_charge=True,
        capital_cost=costs.at["battery", "capital_cost"],
        weight=1.0,
    )

    return n


def attach_ads_renewables(n, plants_df, renewable_carriers, extendable_carriers, costs):
    ads_renewables_path = snakemake.input.ads_renewables

    for tech_type in renewable_carriers:
        plants_filt = plants_df.query("carrier == @tech_type")
        plants_filt.index = plants_filt.ads_name.astype(str)

        logger.info(f"Adding {len(plants_filt)} {tech_type} generators to the network.")

        if tech_type in ["wind", "offwind"]: 
            profiles = pd.read_csv(ads_renewables_path + "/wind_2032.csv", index_col=0)
        elif tech_type == "solar":
            profiles = pd.read_csv(ads_renewables_path + "/solar_2032.csv", index_col=0)
            dpv = pd.read_csv(ads_renewables_path + "/btm_solar_2032.csv", index_col=0)
            profiles = pd.concat([profiles, dpv], axis = 1)
            # plants_filt = plants_filt.dropna(subset=['dispatchshapename']) # dropping the two Solar + Storage plants without dispatch shapes (only the storage plants get dropped)... 
        else:
            profiles = pd.read_csv(ads_renewables_path + f'/{tech_type}_2032.csv', index_col=0)
        
        profiles.columns = profiles.columns.str.replace('.dat: 2032','')
        profiles.columns = profiles.columns.str.replace('.DAT: 2032','')

        profiles.index = n.snapshots
        profiles.columns = profiles.columns.astype(str)

        if tech_type == 'hydro': #matching hydro according to balancing authority specified
            profiles.columns = profiles.columns.str.replace('HY_','')
            profiles.columns = profiles.columns.str.replace('_2018','')
            southwest = {'Arizona', 'SRP', 'WALC', 'TH_Mead'}
            northwest = {'DOPD', 'CHPD', 'WAUW'}
            pge_dict = {'CISO-PGAE':'CIPV', 'CISO-SCE':'CISC', 'CISO-SDGE':'CISD'}  
            plants_filt.balancing_area = plants_filt.balancing_area.map(pge_dict).fillna(plants_filt.balancing_area)
            # {'Arizona', 'CISC', 'IPFE', 'DOPD', 'CISD', 'IPMV', 'CHPD', 'PSCO', 'CISO-SDGE', 'IPTV', 'CIPV', 'TH_Mead', 'CIPB', 'WALC', 'CISO-SCE', 'WAUW', 'SRP', 'CISO-PGAE'}
            #TODO: #34 Add BCHA and AESO hydro profiles in ADS Configuration. Profiles that don't get used: 'AESO', 'IPCO', 'NEVP', 'BCHA'
            # profiles_ba = set(profiles.columns) # available ba hydro profiles
            # bas = set(plants_filt.balancing_area.unique()) # plants that need BA hydro profiles

            # print( need to assign bas for pge bay and valley)
            profiles_new = pd.DataFrame(index=n.snapshots, columns=plants_filt.index)
            for plant in profiles_new.columns:
                ba = plants_filt.loc[plant].balancing_area
                if ba in southwest:
                    ba = 'SouthConsolidated'
                elif ba in northwest:
                    ba = 'BPAT' # this is a temp fix. Probably not right to assign all northwest hydro to BPA
                ba_prof = profiles.columns.str.contains(ba)
                if ba_prof.sum() == 0:
                    logger.warning(f'No hydro profile for {ba}.')
                    profiles_new[plant] = 0


                profiles_new[plant] = profiles.loc[:,ba_prof].values
            p_max_pu = profiles_new
            p_max_pu.columns = plants_filt.index
        else: #  solar + wind + other
            # intersection = set(profiles.columns).intersection(plants_filt.dispatchshapename)
            # missing = set(plants_filt.dispatchshapename) - intersection
            # profiles = profiles[list(intersection)]
            profiles_new = pd.DataFrame(index=n.snapshots, columns=plants_filt.dispatchshapename)
            for plant in profiles_new.columns:
                profiles_new[plant] = profiles[plant]
            p_max_pu = profiles_new
            p_max_pu.columns = plants_filt.index

        p_nom = plants_filt['maxcap(mw)']
        n.madd(
            "Generator",
            plants_filt.index,
            bus=plants_filt.bus_assignment,
            p_nom_min=p_nom,
            p_nom=p_nom,
            marginal_cost=0, #(MMBTu/MW) * (USD/MMBTu) = USD/MW
            # marginal_cost_quadratic = tech_plants.GenIOC * tech_plants.GenFuelCost, 
            capital_cost=costs.at[tech_type, "capital_cost"],
            p_max_pu=p_max_pu, #timeseries of max power output pu
            p_nom_extendable= tech_type in extendable_carriers["Generator"],
            carrier=tech_type,
            weight=1.0,
            efficiency=costs.at[tech_type, "efficiency"],
        )
    return n
########################### Load Data #########################################


########### New Refactored Functions #############

# def attach_wind_and_solar():
#     return

# def attach_hydro():
#     return



def normed(s):
    """
    Normalize a pandas.Series to sum to 1.
    """
    return s / s.sum()


def calculate_annuity(n, r):
    """
    Calculate the annuity factor for an asset with lifetime n years and.

    discount rate of r, e.g. annuity(20, 0.05) * 20 = 1.6
    """
    if isinstance(r, pd.Series):
        return pd.Series(1 / n, index=r.index).where(
            r == 0, r / (1.0 - 1.0 / (1.0 + r) ** n)
        )
    elif r > 0:
        return r / (1.0 - 1.0 / (1.0 + r) ** n)
    else:
        return 1 / n


def add_missing_carriers(n, carriers):
    """
    Function to add missing carriers to the network without raising errors.
    """
    missing_carriers = set(carriers) - set(n.carriers.index)
    if len(missing_carriers) > 0:
        n.madd("Carrier", missing_carriers)


def add_missing_fuel_cost(plants, costs_fn):
    fuel_cost = pd.read_csv(costs_fn, index_col=0,skiprows=3)
    plants['fuel_cost'] = plants.fuel_type.map(fuel_cost.fuel_price_per_mmbtu)
    return plants


def add_missing_heat_rates(plants, heat_rates_fn):
    heat_rates = pd.read_csv(heat_rates_fn, index_col=0, skiprows=3)
    hr_mapped = plants.fuel_type.map(heat_rates.heat_rate_btu_per_kwh) / 1000  #convert to mmbtu/mwh
    plants['inchr2(mmbtu/mwh)'].fillna(hr_mapped, inplace=True)
    return plants


def match_plant_to_bus(n, plants):
    import geopandas as gpd
    from shapely.geometry import Point

    plants_matched = plants.copy()
    plants_matched['bus_assignment'] = None

    buses = n.buses.copy()
    buses['geometry'] = gpd.points_from_xy(buses["x"], buses["y"])

    # from: https://stackoverflow.com/questions/58893719/find-nearest-point-in-other-dataframe-with-a-lot-of-data
    from sklearn.neighbors import BallTree
    # Create a BallTree 
    tree = BallTree(buses[['x', 'y']].values, leaf_size=2)
    # Query the BallTree on each feature from 'appart' to find the distance
    # to the nearest 'pharma' and its id
    plants_matched['distance_nearest'], plants_matched['id_nearest'] = tree.query(
        plants_matched[['longitude', 'latitude']].values, # The input array for the query
        k=1, # The number of nearest neighbors
    )
    plants_matched.bus_assignment = buses.reset_index().iloc[plants_matched.id_nearest].Bus.values
    plants_matched.drop(columns=['id_nearest'], inplace=True)

    return plants_matched


def attach_renewable_capacities_to_atlite(n, plants_df, renewable_carriers):
    plants = plants_df.query(
        "bus_assignment in @n.buses.index"
    )
    for tech in renewable_carriers:
        plants_filt = plants.query("carrier == @tech")
        if plants_filt.empty: continue
        network_gens = n.generators[n.generators.carrier == tech] 
        caps = plants_filt.groupby("bus_assignment").sum().p_nom #namplate capacity per bus
        # caps = caps / gens_per_bus.reindex(caps.index, fill_value=1) ##REVIEW do i need this
        #TODO: #16 Gens excluded from atlite profiles bc of landuse/etc will not be able to be attached if in the breakthrough network
        if caps[~caps.index.isin(network_gens.bus)].sum() > 0:
            missing_capacity = caps[~caps.index.isin(network_gens.bus)].sum()
            logger.info(f"There are {np.round(missing_capacity,1)} MW of {tech} plants that are not in the network. See git issue #16.")
        n.generators.p_nom.update(network_gens.bus.map(caps).dropna())
        n.generators.p_nom_min.update(network_gens.bus.map(caps).dropna())
        logger.info(f"Adding {len(plants_filt)} {tech} generator capacities to the network.")


def update_transmission_costs(n: pypsa.Network, costs, length_factor=1.0):
    # TODO: line length factor of lines is applied to lines and links.
    # Separate the function to distinguish 

    n.lines["capital_cost"] = (
        n.lines["length"] * length_factor * costs.at["HVAC overhead", "capital_cost"]
    )

    if n.links.empty:
        return

    dc_b = n.links.carrier == "DC"

    # If there are no dc links, then the 'underwater_fraction' column
    # may be missing. Therefore we have to return here.
    if n.links.loc[dc_b].empty:
        return

    costs = (
        n.links.loc[dc_b, "length"]
        * length_factor
        * (
            (1.0 - n.links.loc[dc_b, "underwater_fraction"])
            * costs.at["HVDC overhead", "capital_cost"]
            + n.links.loc[dc_b, "underwater_fraction"]
            * costs.at["HVDC submarine", "capital_cost"]
        )
        + costs.at["HVDC inverter pair", "capital_cost"]
    )
    n.links.loc[dc_b, "capital_cost"] = costs


def prepare_breakthrough_demand_data(n: pypsa.Network, 
                                     demand_path: str) -> pd.DataFrame:
    logger.info("Adding Breakthrough Energy Network Demand data from 2016")
    demand = pd.read_csv(demand_path, index_col=0)
    demand.columns = demand.columns.astype(int)
    demand.index = n.snapshots
    intersection = set(demand.columns).intersection(n.buses.zone_id.unique())
    demand = demand[list(intersection)]
    n.buses.rename(columns={'zone_id': 'load_dissag'}, inplace=True)
    return disaggregate_demand_to_buses(n, demand)


def prepare_ads_demand(n: pypsa.Network, 
                       demand_path: str) -> pd.DataFrame:
    demand = pd.read_csv(demand_path, index_col=0)
    data_year = 2032
    demand.columns = demand.columns.str.removeprefix('Load_')
    demand.columns = demand.columns.str.removesuffix('.dat')
    demand.columns = demand.columns.str.removesuffix(f'_{data_year}')
    demand.columns = demand.columns.str.removesuffix(f'_[18].dat: {data_year}')
    demand['CISO-PGAE'] = demand.pop('CIPV') + demand.pop('CIPB') + demand.pop('SPPC') #TODO: #37 Create new Zone for SPPC
    demand['BPAT'] = demand.pop('BPAT') + demand.pop('TPWR') + demand.pop('SCL')
    demand['IPCO'] = demand.pop('IPFE') + demand.pop('IPMV') + demand.pop('IPTV')
    demand['PACW'] = demand.pop('PAID') + demand.pop('PAUT') + demand.pop('PAWY')
    demand['Arizona'] = demand.pop('SRP') + demand.pop('AZPS') 
    demand.drop(columns=['Unnamed: 44', 'TH_Malin', 'TH_Mead', 'TH_PV'], inplace=True)
    ba_list_map = {'CISC': 'CISO-SCE', 'CISD': 'CISO-SDGE','VEA': 'CISO-VEA','WAUW': 'WAUW_SPP'}
    demand.rename(columns=ba_list_map,inplace=True)
    demand.set_index('Index',inplace=True)

    demand.index = n.snapshots
    n.buses['load_dissag'] = n.buses.balancing_area.replace({'': 'missing_ba'})
    intersection = set(demand.columns).intersection(n.buses.ba_load_data.unique())
    demand = demand[list(intersection)]
    return disaggregate_demand_to_buses(n, demand)


def prepare_eia_demand(n: pypsa.Network, 
                       demand_path: str) -> pd.DataFrame:
    logger.info('Building Load Data using EIA demand')
    demand = pd.read_csv(demand_path, index_col=0)
    demand.index = pd.to_datetime(demand.index)
    demand = demand.fillna(method='backfill') #tempory solution until we switch back to gridEmission for the cleaned Data
    demand = demand.loc[n.snapshots.intersection(demand.index)] # Only keep demand data for which we want the snapshots of.
    demand.index = n.snapshots

    demand['Arizona'] = demand.pop('SRP') + demand.pop('AZPS')
    n.buses['load_dissag'] = n.buses.balancing_area.replace({'CISO-PGAE': 'CISO', 
                                                             'CISO-SCE': 'CISO', 
                                                             'CISO-VEA': 'CISO', 
                                                             'CISO-SDGE': 'CISO'})
    n.buses['load_dissag'] = n.buses.load_dissag.replace({'': 'missing_ba'})

    intersection = set(demand.columns).intersection(n.buses.load_dissag.unique())
    demand = demand[list(intersection)]
    return disaggregate_demand_to_buses(n, demand)


def disaggregate_demand_to_buses(n: pypsa.Network, 
                                 demand: pd.DataFrame) -> pd.DataFrame:
    """
    Zone power demand is disaggregated to buses proportional to Pd,
    where Pd is the real power demand (MW).
    """
    demand_per_bus_pu = (n.buses.set_index("load_dissag").Pd / n.buses.groupby("load_dissag").sum().Pd)
    demand_per_bus = demand_per_bus_pu.multiply(demand)
    demand_per_bus.fillna(0, inplace=True)
    demand_per_bus.columns = n.buses.index
    return demand_per_bus


def add_demand_from_file(n: pypsa.Network, 
                         fn_demand: str, 
                         demand_type: str):
    """
    Add demand to network from specified configuration setting. Returns network with demand added.
    """
    if demand_type == "breakthrough":
        demand_per_bus = prepare_breakthrough_demand_data(n, fn_demand)
    elif demand_type == "ads":
        demand_per_bus = prepare_ads_demand(n, fn_demand)
    elif demand_type == "pypsa-usa":
        demand_per_bus = prepare_eia_demand(n, fn_demand)
    else:
        raise ValueError("Invalid demand_type. Supported values are 'breakthrough', 'ads', and 'pypsa-usa'.")
    n.madd("Load", demand_per_bus.columns, bus=demand_per_bus.columns,
           p_set=demand_per_bus, carrier='AC')


def test_snapshot_year_alignment(sns_year: int, load_year: int, configuration: str):
    if sns_year != load_year:
        raise ValueError(f"""Snapshot start year {sns_year} does not match load year {load_year}
                          required for {configuration} configuration.
                          Please update the snapshot start year in the config file. \n
                            ads requires 2032 \n
                            breakthrough requires 2016 \n
                            \n
                          """)

#double check if we need to filter the plants by buses that are in the network filtered by interconnect.
def attach_conventional_generators(
    n,
    costs,
    ppl,
    conventional_carriers,
    extendable_carriers,
    conventional_params,
    conventional_inputs,
    unit_commitment=None,
    fuel_price=None,
):
    carriers = list(set(conventional_carriers) | set(extendable_carriers["Generator"]))
    add_missing_carriers(n, carriers)
    add_co2_emissions(n, costs, carriers)

    # # Replace carrier "natural gas" with the respective technology (OCGT or
    # # CCGT) to align with PyPSA names of "carriers" and avoid filtering "natural
    # # gas" powerplants in ppl.query("carrier in @carriers")
    # ppl.loc[ppl["carrier"] == "natural gas", "carrier"] = ppl.loc[
    #     ppl["carrier"] == "natural gas", "technology"
    # ]

    ppl = (
        ppl.query("carrier in @carriers")
        .join(costs, on="carrier", rsuffix="_r")
        .rename(index=lambda s: "C" + str(s))
    )
    ppl["efficiency"] = ppl.efficiency.fillna(ppl.efficiency_r)

    if unit_commitment is not None:
        committable_attrs = ppl.carrier.isin(unit_commitment).to_frame("committable")
        for attr in unit_commitment.index:
            default = pypsa.components.component_attrs["Generator"].default[attr]
            committable_attrs[attr] = ppl.carrier.map(unit_commitment.loc[attr]).fillna(
                default
            )
    else:
        committable_attrs = {}

    if fuel_price is not None:
        fuel_price = fuel_price.assign(
            OCGT=fuel_price["gas"], CCGT=fuel_price["gas"]
        ).drop("gas", axis=1)
        missing_carriers = list(set(carriers) - set(fuel_price))
        fuel_price = fuel_price.assign(**costs.fuel[missing_carriers])
        fuel_price = fuel_price.reindex(ppl.carrier, axis=1)
        fuel_price.columns = ppl.index
        marginal_cost = fuel_price.div(ppl.efficiency).add(ppl.carrier.map(costs.VOM))
    else:
        marginal_cost = (
            ppl.carrier.map(costs.VOM) + ppl.carrier.map(costs.fuel) / ppl.efficiency
        )

    # Define generators using modified ppl DataFrame
    caps = ppl.groupby("carrier").p_nom.sum().div(1e3).round(2)
    logger.info(f"Adding {len(ppl)} generators with capacities [GW] \n{caps}")
    n.madd(
        "Generator",
        ppl.index,
        carrier=ppl.carrier,
        bus=ppl.bus_assignment,
        p_nom_min=ppl.p_nom.where(ppl.carrier.isin(conventional_carriers), 0),
        p_nom=ppl.p_nom.where(ppl.carrier.isin(conventional_carriers), 0),
        p_nom_extendable=ppl.carrier.isin(extendable_carriers["Generator"]),
        efficiency=ppl.efficiency,
        marginal_cost=marginal_cost,
        capital_cost=ppl.capital_cost,
        build_year=ppl.build_year.fillna(0).astype(int),
        lifetime=(ppl.dateout - ppl.build_year).fillna(np.inf),
        **committable_attrs,
    )

    # for carrier in set(conventional_params) & set(carriers):
    #     # Generators with technology affected
    #     idx = n.generators.query("carrier == @carrier").index

    #     for attr in list(set(conventional_params[carrier]) & set(n.generators)):
    #         values = conventional_params[carrier][attr]

    #         if f"conventional_{carrier}_{attr}" in conventional_inputs:
    #             # Values affecting generators of technology k country-specific
    #             # First map generator buses to countries; then map countries to p_max_pu
    #             values = pd.read_csv(
    #                 snakemake.input[f"conventional_{carrier}_{attr}"], index_col=0
    #             ).iloc[:, 0]
    #             bus_values = n.buses.country.map(values)
    #             n.generators[attr].update(
    #                 n.generators.loc[idx].bus.map(bus_values).dropna()
    #             )
    #         else:
    #             # Single value affecting all generators of technology k indiscriminantely of country
    #             n.generators.loc[idx, attr] = values


def attach_battery_storage(n: pypsa.Network, 
                           plants: pd.DataFrame, 
                           extendable_carriers, 
                           costs):
    """Attaches Battery Energy Storage Systems To the Network.
    """
    plants_filt = plants.query("carrier == 'battery' ")
    plants_filt.index = plants_filt.index.astype(str) + "_" + plants_filt.generator_id.astype(str)

    logger.info(f"Adding {len(plants_filt)} Batteries as Stores to the network.")
    plants_filt = plants_filt.dropna(subset=['energy_capacity_mwh'])
    # logger.info(f"Dropping {len(plants_filt) - len(plants_filt.dropna(subset=['energy_capacity_mwh']))} Batteries without energy capacity.")

    n.madd(
        "StorageUnit",
        plants_filt.index,
        carrier = "battery",
        bus=plants_filt.bus_assignment,
        p_nom=plants_filt.p_nom,
        p_nom_extendable='battery' in extendable_carriers['Store'],
        max_hours = plants_filt.energy_capacity_mwh / plants_filt.p_nom,
        build_year=plants_filt.operating_year,
        efficiency_store= 1.0,
        efficiency_dispatch=1.0, #TODO: Add efficiency_dispatch to config file
        cyclic_state_of_charge=True,
        capital_cost=costs.at["battery", "capital_cost"],
    )


def load_powerplants_eia(
    eia_dataset: str, 
    carrier_mapper: Dict[str,str] = None,
) -> pd.DataFrame:
    # load data
    plants = pd.read_csv(
        eia_dataset, 
        index_col=0, 
        dtype={"bus_assignment": "str"}).rename(columns=str.lower)

    # apply mappings if required 
    if carrier_mapper:
        plants['carrier'] = plants.tech_type.map(carrier_mapper)    

    plants = add_missing_fuel_cost(plants, snakemake.input.fuel_costs)
    plants = add_missing_heat_rates(plants, snakemake.input.fuel_costs)
    plants['generator_name'] = plants.index.astype(str) + "_" + plants.generator_id.astype(str)
    plants.set_index('generator_name', inplace=True)
    plants['p_nom'] = plants.pop('capacity_mw')
    plants['heat_rate'] = plants.pop('inchr2(mmbtu/mwh)')
    plants['marginal_cost'] = plants.heat_rate * plants.fuel_cost  #(MMBTu/MW) * (USD/MMBTu) = USD/MW
    plants['efficiency'] = 1 / (plants['heat_rate'] / 3.412) #MMBTu/MWh to MWh_electric/MWh_thermal
    plants['ramp_limit_up'] = plants.pop('rampup rate(mw/minute)') / plants.p_nom * 60 #MW/min to p.u./hour
    plants['ramp_limit_down'] = plants.pop('rampdn rate(mw/minute)') / plants.p_nom * 60 #MW/min to p.u./hour
    plants['build_year'] = plants.operating_year
    plants['dateout'] = np.inf #placeholder TODO FIX LIFETIME
    return plants


def load_powerplants_ads(
    ads_dataset: str, 
    tech_mapper: Dict[str,str] = None,
    carrier_mapper: Dict[str,str] = None,
    fuel_mapper: Dict[str,str] = None
) -> pd.DataFrame:
    """Loads base ADS plants and applies name mappings 
    
    Arguments
    ---------
    ads_dataset: str, 
    tech_mapper: Dict[str,str],
    carrier_mapper: Dict[str,str],
    fuel_mapper: Dict[str,str],
    """

    # read in data 
    plants = pd.read_csv(ads_dataset, index_col=0, dtype={"bus_assignment": "str"}).rename(columns=str.lower)
    plants.rename(columns={'fueltype':'fuel_type_ads'}, inplace=True)

    # apply mappings if required 
    if carrier_mapper:
        plants['carrier'] = plants.fuel_type_ads.map(carrier_mapper)
    if fuel_mapper:
        plants['fuel_type'] = plants.fuel_type_ads.map(fuel_mapper)
    if tech_mapper:
        plants['tech_type'] = plants.tech_type.map(tech_mapper)
    plants.rename(columns={'lat':'latitude', 'lon':'longitude'}, inplace=True)    

    plants['generator_name'] = plants.ads_name.astype(str)
    plants['p_nom'] = plants['maxcap(mw)']
    plants['heat_rate'] = plants['inchr2(mmbtu/mwh)']
    plants['marginal_cost'] = plants['heat_rate'] * plants.fuel_cost  #(MMBTu/MW) * (USD/MMBTu) = USD/MW
    plants['efficiency'] = 1 / (plants['heat_rate'] / 3.412) #MMBTu/MWh to MWh_electric/MWh_thermal
    plants['ramp_limit_up'] = plants['rampup rate(mw/minute)']/ plants['maxcap(mw)'] * 60 #MW/min to p.u./hour
    plants['ramp_limit_down'] = plants['rampdn rate(mw/minute)']/ plants['maxcap(mw)'] * 60 #MW/min to p.u./hour
    return plants


def load_powerplants_breakthrough(breakthrough_dataset: str) -> pd.DataFrame:
    """Loads base Breakthrough Energy plants and applies name mappings"""

    plants = pd.read_csv(breakthrough_dataset, dtype={"bus_id": str}, index_col=0).query("bus_id in @n.buses.index")
    plants.replace(["dfo"], ["oil"], inplace=True)
    plants['generator_name'] = plants.index.astype(str)
    plants['bus_assignment']= plants.bus_id.astype(str)
    plants['p_nom']= plants['Pmax']
    plants['heat_rate']= plants['GenIOB']
    plants['marginal_cost']= plants['GenIOB'] * plants['GenFuelCost']  #(MMBTu/MW) * (USD/MMBTu) = USD/MW
    plants['efficiency']= 1 / (plants['GenIOB'] / 3.412) #MMBTu/MWh to MWh_electric/MWh_thermal
    plants['carrier']= plants.type
    return plants


def attach_wind_and_solar(
    n: pypsa.Network, 
    costs: pd.DataFrame, 
    input_profiles: str, 
    carriers: list[str],
    extendable_carriers: Dict[str, list[str]],
    line_length_factor=1
    ):
    add_missing_carriers(n, carriers)
    for car in carriers:
        if car == "hydro":
            continue

        with xr.open_dataset(getattr(input_profiles, "profile_" + car)) as ds:
            if ds.indexes["bus"].empty:
                continue

            supcar = car.split("-", 2)[0]
            if supcar == "offwind":
                underwater_fraction = ds["underwater_fraction"].to_pandas()
                connection_cost = (
                    line_length_factor
                    * ds["average_distance"].to_pandas()
                    * (
                        underwater_fraction
                        * costs.at[car + "-connection-submarine", "capital_cost"]
                        + (1.0 - underwater_fraction)
                        * costs.at[car + "-connection-underground", "capital_cost"]
                    )
                )
                capital_cost = (
                    costs.at["offwind", "capital_cost"]
                    + costs.at[car + "-station", "capital_cost"]
                    + connection_cost
                )
                logger.info(
                    "Added connection cost of {:0.0f}-{:0.0f} USD/MW/a to {}".format(
                        connection_cost.min(), connection_cost.max(), car
                    )
                )
            else:
                capital_cost = costs.at[car, "capital_cost"]

            bus2sub = pd.read_csv(input_profiles.bus2sub, dtype=str).drop("interconnect", axis=1)
            bus_list = ds.bus.to_dataframe("sub_id").merge(bus2sub).bus_id.astype(str).values
            p_nom_max_bus = ds["p_nom_max"].to_dataframe().merge(bus2sub,left_on="bus", right_on="sub_id").set_index('bus_id').p_nom_max
            weight_bus = ds["weight"].to_dataframe().merge(bus2sub,left_on="bus", right_on="sub_id").set_index('bus_id').weight
            bus_profiles = ds["profile"].transpose("time", "bus").to_pandas().T.merge(bus2sub,left_on="bus", right_on="sub_id").set_index('bus_id').drop(columns='sub_id').T
            
            logger.info(f"Adding {car} capacity-factor profiles to the network.")
            #TODO: #24 VALIDATE TECHNICAL POTENTIALS

            n.madd(
                "Generator",
                bus_list,
                " " + car,
                bus=bus_list,
                carrier=car,
                p_nom_extendable=car in extendable_carriers["Generator"],
                p_nom_max=p_nom_max_bus,
                weight=weight_bus,
                marginal_cost=costs.at[supcar, "marginal_cost"],
                capital_cost=capital_cost,
                efficiency=costs.at[supcar, "efficiency"],
                p_max_pu=bus_profiles,
            )



#########################################
if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake
        snakemake = mock_snakemake("add_electricity", interconnect='western')
    configure_logging(snakemake)

    params = snakemake.params
    configuration = snakemake.config["network_configuration"]
    n = pypsa.Network(snakemake.input.base_network)
    n.name = configuration
    
    snapshot_config = snakemake.config['snapshots']
    sns_start = pd.to_datetime(snapshot_config['start'] + ' 07:00:00') 
    # Shifting to 7am UTC since this is the Midnight PST time. 
    # Work around to deal with the fact that the load data is in UTC and n.snapshots is in UTC... but annual EIA file doesnt include first 7 UTC hours of the year.... 
    # TODO: Need to combine all EIA data into one file then filter based on n.snapshots
    sns_end = pd.to_datetime(snapshot_config['end'] + ' 00:00:00')
    sns_inclusive = snapshot_config['inclusive']

    if configuration  == "pypsa-usa":
        fn_demand = snakemake.input['eia'][sns_start.year%2017]
    elif configuration  == "ads2032":
        ads_year = 2032
        fn_demand = f'data/WECC_ADS/processed/load_{ads_year}.csv'
        test_snapshot_year_alignment(sns_start.year, ads_year, configuration)
    elif configuration  == "breakthrough":
        test_snapshot_year_alignment(sns_start.year, 2016, configuration)
        fn_demand = snakemake.input["demand_breakthrough_2016"]
    else:
        raise ValueError("Invalid demand_type. Supported values are 'breakthrough', 'ads', and 'eia'.")
    
    n.set_snapshots(pd.date_range(freq="h", 
                            start= sns_start,
                            end= sns_end,
                            inclusive= sns_inclusive)
                    )

    Nyears = n.snapshot_weightings.objective.sum() / 8760.0

    costs = load_costs(
        snakemake.input.tech_costs,
        params.costs,
        params.electricity["max_hours"],
        Nyears,
    )

    update_transmission_costs(n, costs, params.length_factor)

    renewable_carriers = set(params.electricity["renewable_carriers"])
    extendable_carriers = params.electricity["extendable_carriers"]
    conventional_carriers = params.electricity["conventional_carriers"]
    conventional_inputs = {
        k: v for k, v in snakemake.input.items() if k.startswith("conventional_")
    }
    
    if params.conventional["unit_commitment"]:
        unit_commitment = pd.read_csv(snakemake.input.unit_commitment, index_col=0)
    else:
        unit_commitment = None

    if params.conventional["dynamic_fuel_price"]:
        fuel_price = pd.read_csv(
            snakemake.input.fuel_price, index_col=0, header=0, parse_dates=True
        )
        fuel_price = fuel_price.reindex(n.snapshots).fillna(method="ffill")
    else:
        fuel_price = None

    add_demand_from_file(n, fn_demand, configuration)

    if configuration == "pypsa-usa": 
        import pdb; pdb.set_trace()
        costs = costs.rename(index={"onwind": "wind",
                                     #"OCGT": "ng"
                                     }) #changing cost data to match the plant data #TODO: #10 change this so that fuel types and plant types match the pypsa naming scheme.

        eia_carrier_mapper = constants.EIA_CARRIER_MAPPER
        plants = load_powerplants_eia(snakemake.input['plants_eia'], 
                                      eia_carrier_mapper)
        plants = match_plant_to_bus(n, plants)
        attach_conventional_generators(
            n,
            costs,
            plants,
            conventional_carriers,
            extendable_carriers,
            params.conventional,
            conventional_inputs,
            unit_commitment=unit_commitment,
            fuel_price=fuel_price
        )
        attach_battery_storage(
            n, 
            plants,
            extendable_carriers, 
            costs
        )
        #attach renewable plants to network
        costs = costs.rename(index={"offwind-ac-connection-submarine": "offwind-connection-submarine",
                                    "offwind-ac-connection-underground": "offwind-connection-underground",
                                    'offwind-ac-station': 'offwind-station',
                                    "onwind":"wind"
                                    }) #temporary fix. should rename carriers instead of changing cost names. w TODO#10
    
        attach_wind_and_solar(
            n,
            costs,
            snakemake.input,
            renewable_carriers,
            extendable_carriers,
            params.length_factor,
        )

        renewable_carriers = list(
            set(snakemake.config['electricity']["renewable_carriers"]).intersection(
                set(["wind", "solar", "offwind"])
            )
        )
        attach_renewable_capacities_to_atlite(
            n,
            plants,
            renewable_carriers
        )

        update_p_nom_max(n)

        #temporarily adding hydro with breakthrough only data until I can correctly import hydro_data
        renewable_carriers = list(
            set(snakemake.config['electricity']["renewable_carriers"]).intersection(
                set(["hydro"])
            )
        )
        n = attach_breakthrough_renewable_plants(
            n,
            snakemake.input["plants"],
            renewable_carriers,
            extendable_carriers,
            costs,
        )
    elif configuration == "breakthrough":
        costs = costs.rename(index={"onwind": "wind", "OCGT": "ng"}) #changing cost data to match the breakthrough plant data #TODO: #10 change this so that breakthrough fuel types and plant types match the pypsa naming scheme.
        conventional_carriers = list(
            set(snakemake.config['electricity']["conventional_carriers"]).intersection(
                set(["coal", "ng", "nuclear", "oil", "geothermal"])
            )
        )

        n = attach_breakthrough_conventional_plants(
            n,
            snakemake.input["plants"],
            conventional_carriers,
            extendable_carriers,
            costs,
        )

        #adding breakthrough renewable plants to network
        costs = costs.rename(index={"offwind-ac-connection-submarine": "offwind-connection-submarine",
                                    "offwind-ac-connection-underground": "offwind-connection-underground",
                                    'offwind-ac-station': 'offwind-station',
                                    "onwind":"wind"}) #temporary fix. should rename carriers instead of changing cost names. w TODO#10
        attach_wind_and_solar(
            n,
            costs,
            snakemake.input,
            renewable_carriers,
            extendable_carriers,
            params.length_factor,
        )

        renewable_carriers = list(
            set(snakemake.config['electricity']["renewable_carriers"]).intersection(
                set(["wind", "solar", "offwind"])
            )
        )

        attach_breakthrough_renewable_capacities_to_atlite(n, snakemake.input["plants"], renewable_carriers)
        update_p_nom_max(n)

        #temporarily adding hydro with breakthrough only data until I can correctly import hydro_data
        renewable_carriers = list(
            set(snakemake.config['electricity']["renewable_carriers"]).intersection(
                set(["hydro"])
            )
        )
        n = attach_breakthrough_renewable_plants(
            n,
            snakemake.input["plants"],
            renewable_carriers,
            extendable_carriers,
            costs,
        )
    elif configuration == "ads2032": 
        
        # get mappers 
        ads_tech_mapper = constants.ADS_TECH_MAPPER
        ads_sub_type_tech_mapper = constants.ADS_SUB_TYPE_TECH_MAPPER
        ads_carrier_mapper = constants.ADS_CARRIER_NAME
        ads_fuel_mapper = constants.ADS_FUEL_MAPPER

        # load base powerplants 
        plants = load_powerplants_ads(
            ads_dataset = snakemake.input['plants_ads'],
            tech_mapper = ads_sub_type_tech_mapper, 
            carrier_mapper = ads_carrier_mapper,
            fuel_mapper = ads_fuel_mapper
        )
        
        # apply missing data to powerplants 
        plants = add_missing_fuel_cost(plants, snakemake.input.fuel_costs)
        plants = add_missing_heat_rates(plants, snakemake.input.fuel_costs)
        
        #assign coords to plants missing lat/lon
        plants = assign_ads_missing_lat_lon(plants,n)

        #match each plant to nearest node in network
        plant_with_bus_assignments = match_plant_to_bus(n, plants)

        #attach conventional plants to network
        n = attach_ads_conventional_plants(
            n,
            plant_with_bus_assignments,
            conventional_carriers,
            extendable_carriers,
            costs,
        )

        #attach batteries to network
        n = attach_ads_batteries(
            n, 
            plant_with_bus_assignments,
            extendable_carriers, 
            costs
        )

        #attach renewable plants to network
        costs = costs.rename(index={"offwind-ac-connection-submarine": "offwind-connection-submarine",
                                    "offwind-ac-connection-underground": "offwind-connection-underground",
                                    'offwind-ac-station': 'offwind-station',
                                    "onwind":"wind"
                                    }) #temporary fix. should rename carriers instead of changing cost names. w TODO#10

        attach_ads_renewables(
            n,
            plant_with_bus_assignments,
            renewable_carriers,
            extendable_carriers,
            costs,
        )
        update_p_nom_max(n)
    else:
        raise ValueError(f"Unknown network_configuration {snakemake.config['network_configuration']}")

    if snakemake.config['osw_config']['enable_osw']:
        logger.info('Adding OSW in network')
        humboldt_capacity = snakemake.config['osw_config']['humboldt_capacity']
        import modify_network_osw as osw
        osw.build_OSW_base_configuration(n, osw_capacity=humboldt_capacity)
        if snakemake.config['osw_config']['build_hvac']: osw.build_OSW_500kV(n)
        if snakemake.config['osw_config']['build_hvdc_subsea']: osw.build_hvdc_subsea(n)
        if snakemake.config['osw_config']['build_hvdc_overhead']: osw.build_hvdc_overhead(n)

    sanitize_carriers(n, snakemake.config)
    n.meta = snakemake.config
    n.export_to_netcdf(snakemake.output[0])

    output_folder = os.path.dirname(snakemake.output[0]) + '/base_network'
    export_network_for_gis_mapping(n, output_folder)






# def attach_eia_batteries(n, plants_df,extendable_carriers, costs):
#     plants = plants_df.query(
#         "bus_assignment in @n.buses.index"
#     )
#     plants_filt = plants.query("carrier == 'battery' ")
#     plants_filt.index = plants_filt.index.astype(str) + "_" + plants_filt.generator_id.astype(str)
#     n.madd(
#         "StorageUnit",
#         plants_filt.index,
#         bus=plants_filt.bus_assignment,
#         p_nom=plants_filt.capacity_mw,
#         p_nom_extendable='battery' in extendable_carriers['Store'],
#         max_hours = plants_filt.energy_capacity_mwh / plants_filt.capacity_mw,
#         build_year=plants_filt.operating_year,
#         carrier=plants_filt.carrier,
#         efficiency_store= 1.0,
#         efficiency_dispatch=1.0, #TODO: Add efficiency_dispatch to config file
#         cyclic_state_of_charge=True,
#         capital_cost=costs.at["battery", "capital_cost"],
#     )
#     return n

# def prepare_ads_conventional_plants(
#     plants_df: pd.DataFrame
# ) -> pd.DataFrame:
#     """
#     Arranges ADS conventional plant data into a format that can be used by
#       the add_conventional_plants and maybe also the add_wind_and_solar functions.
#     """
#     plants_filt.index = plants_filt.ads_name.astype(str)
#     bus=plants_filt.bus_assignment
#     p_nom=plants_filt['maxcap(mw)']
#     p_nom_extendable= tech_type in extendable_carriers['Generator']
#     marginal_cost=plants_filt['inchr2(mmbtu/mwh)'] * plants_filt.fuel_cost,  #(MMBTu/MW) * (USD/MMBTu) = USD/MW
#     ramp_limit_up= plants_filt['rampup rate(mw/minute)']/ plants_filt['maxcap(mw)'] * 60, #MW/min to p.u./hour
#     ramp_limit_down= plants_filt['rampdn rate(mw/minute)']/ plants_filt['maxcap(mw)'] * 60, #MW/min to p.u./hour
#     carrier=plants_filt.carrier,


# def attach_eia_conventional_plants(
#     plants_df: pd.DataFrame
# ) -> pd.DataFrame:
#     """
#     Arranges EIA conventional plant data into a format that can be used by
#       the add_conventional_plants and maybe also the add_wind_and_solar functions.
#     """
#     plants_filt.index = plants_filt.index.astype(str) + "_" + plants_filt.generator_id.astype(str)
#     bus=plants_filt.bus_assignment
#     p_nom=plants_filt.capacity_mw
#     p_nom_extendable= tech_type in extendable_carriers['Generator']
#     marginal_cost=plants_filt['inchr2(mmbtu/mwh)'] * plants_filt.fuel_cost  #(MMBTu/MW) * (USD/MMBTu) = USD/MW
#     ramp_limit_up= plants_filt['rampup rate(mw/minute)']/ plants_filt.capacity_mw * 60 #MW/min to p.u./hour
#     ramp_limit_down= plants_filt['rampdn rate(mw/minute)']/ plants_filt.capacity_mw * 60 #MW/min to p.u./hour
#     carrier=plants_filt.carrier
#     build_year=plants_filt.operating_year
#     weight=1.0
# def attach_breakthrough_conventional_plants(fn_plants):
#     # _add_missing_carriers_from_costs(n, costs, conventional_carriers)
#     # plants = pd.read_csv(fn_plants, dtype={"bus_id": str}, index_col=0).query("bus_id in @n.buses.index")
#     # plants.replace(["dfo"], ["oil"], inplace=True)

#     tech_plants.index = tech_plants.index.astype(str)
#     bus=tech_plants.bus_id.astype(str),
#     p_nom=tech_plants.Pmax,
#     p_nom_extendable= tech in extendable_carriers["Generator"],
#     marginal_cost=tech_plants.GenIOB * tech_plants.GenFuelCost,  #(MMBTu/MW) * (USD/MMBTu) = USD/MW
#     marginal_cost_quadratic= tech_plants.GenIOC * tech_plants.GenFuelCost,
#     carrier=tech_plants.type,
#     weight=1.0,