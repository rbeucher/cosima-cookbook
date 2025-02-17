"""querying.py

Functions for data discovery.

"""

import logging
import os.path
import pandas as pd
from sqlalchemy import func, distinct, or_
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import subquery
import warnings
import xarray as xr

from . import database
from .database import NCExperiment, NCFile, CFVariable, NCVar, Keyword
from .database import NCAttribute, NCAttributeString


class VariableNotFoundError(Exception):
    pass


class QueryWarning(UserWarning):
    pass


# By default all ambiguous queries will raise an exception
warnings.simplefilter("error", category=QueryWarning, lineno=0, append=False)


def get_experiments(
    session,
    experiment=True,
    keywords=None,
    variables=None,
    all=False,
    exptname=None,
    **kwargs,
):
    """
    Returns a DataFrame of all experiments and the number of netCDF4 files contained
    within each experiment.

    Optionally one or more keywords can be specified, and only experiments with all the
    specified keywords will be return. The keyword strings can utilise SQL wildcard
    characters, "%" and "_", to match multiple keywords.

    Optionally variables can also be specified, and only experiments containing all those
    variables will be returned.

    All metadata fields will be returned if all=True, or individual metadata fields
    can be selected by passing field=True, where available fields are:
    contact, email, created, description, notes, url and root_dir
    """

    # Determine which attributes to return. Special case experiment
    # as this is the only one that defaults to True
    columns = []
    if experiment:
        columns.append(NCExperiment.experiment)

    for f in NCExperiment.metadata_keys + ["root_dir"]:
        # Explicitly don't support returning keyword metadata
        if f == "keywords":
            continue
        if kwargs.get(f, all):
            columns.append(getattr(NCExperiment, f))

    q = (
        session.query(*columns, func.count(NCFile.experiment_id).label("ncfiles"))
        .join(NCFile.experiment)
        .group_by(NCFile.experiment_id)
    )

    if keywords is not None:
        if isinstance(keywords, str):
            keywords = [keywords]
        q = q.filter(*(NCExperiment.keywords.like(k) for k in keywords))

    if variables is not None:
        if isinstance(variables, str):
            variables = [variables]

        expt_query = (
            session.query(NCExperiment.id)
            .join(NCFile.experiment)
            .join(NCFile.ncvars)
            .join(NCVar.variable)
            .group_by(NCExperiment.experiment)
            .having(func.count(distinct(CFVariable.name)) == len(variables))
            .filter(CFVariable.name.in_(variables))
        )

        q = q.filter(NCExperiment.id.in_(expt_query))

    if exptname is not None:
        q = q.filter(NCExperiment.experiment == exptname)

    return pd.DataFrame(q, columns=[c["name"] for c in q.column_descriptions])


def get_ncfiles(session, experiment):
    """
    Returns a DataFrame of all netcdf files for a given experiment.
    """

    q = (
        session.query(NCFile.ncfile, NCFile.index_time)
        .join(NCFile.experiment)
        .filter(NCExperiment.experiment == experiment)
        .order_by(NCFile.ncfile)
    )

    return pd.DataFrame(q, columns=[c["name"] for c in q.column_descriptions])


def get_keywords(session, experiment=None):
    """
    Returns a set of all keywords, and optionally only for a given experiment
    """

    if experiment is not None:
        q = session.query(NCExperiment).filter(NCExperiment.experiment == experiment)
        return q.scalar().keywords
    else:
        q = session.query(Keyword)
        return {r.keyword for r in q}


def get_variables(
    session,
    experiment=None,
    frequency=None,
    cellmethods=None,
    inferred=False,
    search=None,
):
    """
    Returns a DataFrame of variables for a given experiment if experiment
    name is specified, and optionally a given diagnostic frequency.
    If inferred is True and some experiment specific properties inferred from other
    fields are also returned: coordinate, model and restart.
           - coordinate: True if coordinate, False otherwise
           - model: model from which variable output, possible values are ocean,
                    atmosphere, land, ice, or none if can't be identified
           - restart: True if variable from a restart file, False otherwise
    If experiment is not specified all variables for all experiments are returned,
    without experiment specific data.
    Specifying an array of search strings will limit variables returned to any
    containing any of the search terms in variable name, long name, or standard name.
    """

    # Default columns
    columns = [
        CFVariable.name,
        CFVariable.long_name,
        CFVariable.units,
    ]

    if experiment:

        # Create aliases so as to able to join to the NCAttribute table
        # twice, for the name and value
        ncas1 = aliased(NCAttributeString)
        ncas2 = aliased(NCAttributeString)
        subq = (
            session.query(
                NCAttribute.ncvar_id.label("ncvar_id"),
                ncas2.value.label("value"),
            )
            .join(ncas1, NCAttribute.name_id == ncas1.id)
            .join(ncas2, NCAttribute.value_id == ncas2.id)
            .filter(ncas1.value == "cell_methods")
        ).subquery(name="attrs")

        columns.extend(
            [
                NCFile.frequency,
                NCFile.ncfile,
                subq.c.value.label("cell_methods"),
                func.count(NCFile.ncfile).label("# ncfiles"),
                func.min(NCFile.time_start).label("time_start"),
                func.max(NCFile.time_end).label("time_end"),
            ]
        )

    if inferred:
        # Return inferred information
        columns.extend(
            [
                CFVariable.is_coordinate.label("coordinate"),
                NCFile.model,
                NCFile.is_restart.label("restart"),
            ]
        )

    # Base query
    q = (
        session.query(*columns)
        .join(NCFile.experiment)
        .join(NCFile.ncvars)
        .join(NCVar.variable)
    )

    if experiment is not None:
        # Join against the NCAttribute table above. Outer join ensures
        # variables without cell_methods attribute still appear with NULL
        q = q.outerjoin(subq, subq.c.ncvar_id == NCVar.id)

    q = q.order_by(NCFile.frequency, CFVariable.name, NCFile.time_start, NCFile.ncfile)
    q = q.group_by(CFVariable, NCFile.frequency)

    if experiment is not None:
        q = q.group_by(subq.c.value)
        q = q.filter(NCExperiment.experiment == experiment)

        # Filtering on frequency only makes sense if experiment is specified
        if frequency is not None:
            q = q.filter(NCFile.frequency == frequency)

        # Filtering on cell methods only makes sense if experiment is specified
        if cellmethods is not None:
            q = q.filter(subq.c.value == cellmethods)

    if search is not None:
        # Filter based on search term appearing in name, long_name or standard_name
        if isinstance(search, str):
            search = [
                search,
            ]
        q = q.filter(
            or_(
                column.contains(word)
                for word in search
                for column in (
                    CFVariable.name,
                    CFVariable.long_name,
                    CFVariable.standard_name,
                )
            )
        )

    default_dtypes = {
        "# ncfiles": "int64",
        "coordinate": "boolean",
        "model": "category",
        "restart": "boolean",
    }

    df = pd.DataFrame(q, columns=[c["name"] for c in q.column_descriptions])

    return df.astype({k: v for k, v in default_dtypes.items() if k in df.columns})


def get_frequencies(session, experiment=None):
    """
    Returns a DataFrame with all diagnostics frequencies and optionally
    for a given experiment.
    """

    if experiment is None:
        q = session.query(NCFile.frequency).group_by(NCFile.frequency)
    else:
        q = (
            session.query(NCFile.frequency)
            .join(NCFile.experiment)
            .filter(NCExperiment.experiment == experiment)
            .group_by(NCFile.frequency)
        )

    return pd.DataFrame(q, columns=[c["name"] for c in q.column_descriptions])


def getvar(
    expt,
    variable,
    session,
    ncfile=None,
    start_time=None,
    end_time=None,
    n=None,
    frequency=None,
    attrs=None,
    attrs_unique=None,
    return_dataset=False,
    **kwargs,
):
    """For a given experiment, return an xarray DataArray containing the
    specified variable.

    expt - text string indicating the name of the experiment
    variable - text string indicating the name of the variable to load
    session - a database session created by cc.database.create_session()
    ncfile -  an optional text string indicating the pattern for filenames
              to load. All filenames containing this string will match, so
              be specific. '/' can be used to match the start of the
              filename, and '%' is a wildcard character.
    start_time - only load data after this date. specify as a text string,
                 e.g. '1900-01-01'
    end_time - only load data before this date. specify as a text string,
               e.g. '1900-01-01'
    n - after all other queries, restrict the total number of files to the
        first n. pass a negative value to restrict to the last n
    frequency - specify frequency to disambiguate identical variables saved
                at different temporal resolution
    attrs - a dictionary of attribute names and their values that must be
            present on the returned variables
    attrs_unique - a dictionary of attribute names and their values that
            must be unique on the returned variables. Defaults to
            {'cell_methods': 'time: mean'} and should not generally be
            changed.
    return_dataset - if True, return xarray.Dataset, containing the
                     requested variable, along with its time_bounds,
                     if present.  Otherwise (default), return
                     xarray.DataArray containing only the variable

    Note that if start_time and/or end_time are used, the time range
    of the resulting dataset may not be bounded exactly on those
    values, depending on where the underlying files start/end. Use
    dataset.sel() to exactly select times from the dataset.

    Other kwargs are passed through to xarray.open_mfdataset, including:

    chunks - Override any chunking by passing a chunks dictionary.
    decode_times - Time decoding can be disabled by passing decode_times=False

    """

    if attrs_unique is None:
        attrs_unique = {"cell_methods": "time: mean"}

    ncfiles = _ncfiles_for_variable(
        expt,
        variable,
        session,
        ncfile,
        start_time,
        end_time,
        n,
        frequency,
        attrs,
        attrs_unique,
    )

    variables = [variable]
    if return_dataset:
        # we know at least one variable was returned, so we can index ncfiles
        # ask for the extra variables associated with cell_methods, etc.
        variables += _bounds_vars_for_variable(*ncfiles[0])

    # chunking -- use first row/file and assume it's the same across the whole dataset
    xr_kwargs = {"chunks": _parse_chunks(ncfiles[0].NCVar)}
    xr_kwargs.update(kwargs)

    def _preprocess(d):
        if variable in d.coords:
            # just return coordinate data
            return d

        # otherwise, figure out if we need any ancilliary data
        # like time_bounds
        return d[variables]

    ncfiles = list(str(f.NCFile.ncfile_path) for f in ncfiles)

    ds = xr.open_mfdataset(
        ncfiles,
        parallel=True,
        combine="by_coords",
        preprocess=_preprocess,
        **xr_kwargs,
    )

    if return_dataset:
        da = ds
    else:
        # if we want a dataarray, we'll strip off the extra info
        da = ds[variable]

    # Check the chunks given were actually in the data
    chunks = xr_kwargs.get("chunks", None)
    if chunks is not None:
        missing_chunk_dims = set(chunks.keys()) - set(da.dims)
        if len(missing_chunk_dims) > 0:
            logging.warning(
                f"chunking along dimensions {missing_chunk_dims} is not possible. Available dimensions for chunking are {set(da.dims)}"
            )

    da.attrs["ncfiles"] = ncfiles

    # Get experiment metadata, delete extraneous fields and add
    # to attributes
    metadata = get_experiments(
        session, experiment=False, exptname=expt, all=True
    ).to_dict(orient="records")[0]

    metadata = {
        k: v
        for k, v in metadata.items()
        if k not in ["ncfiles", "index", "root_dir"]
        and (v is not None and v != "None" and v != "")
    }

    da.attrs.update(metadata)

    return da


def _bounds_vars_for_variable(ncfile, ncvar):
    """Return a list of names for a variable and its bounds"""

    variables = []

    if "cell_methods" not in ncvar.attrs:
        # no cell methods, so no need to look for bounds
        return variables

    # [cell methods] is a string attribute comprising a list of
    # blank-separated words of the form "name: method"
    cell_methods = iter(ncvar.attrs["cell_methods"].split())

    # for the moment, we're only looking for a time mean
    for dim, method in zip(cell_methods, cell_methods):
        if not (dim[:-1] == "time" and method == "mean"):
            continue

    bounds_var = ncfile.ncvars["time"].attrs.get("bounds")
    if bounds_var is not None:
        variables.append(bounds_var)

    return variables


def _ncfiles_for_variable(
    expt,
    variable,
    session,
    ncfile=None,
    start_time=None,
    end_time=None,
    n=None,
    frequency=None,
    attrs=None,
    attrs_unique=None,
):
    """Return a list of (NCFile, NCVar) pairs corresponding to the
    database objects for a given variable.

    Optionally, pass ncfile, start_time, end_time, frequency, attrs,
    attrs_unique, or n for additional disambiguation (see getvar
    documentation for their semantics).
    """

    if attrs is None:
        attrs = {}

    if attrs_unique is None:
        attrs_unique = {}

    f, v = database.NCFile, database.NCVar
    q = (
        session.query(f, v)
        .join(f.ncvars)
        .join(f.experiment)
        .filter(v.varname == variable)
        .filter(database.NCExperiment.experiment == expt)
        .filter(f.present)
        .order_by(f.time_start)
    )

    # additional disambiguation
    if ncfile is not None:
        q = q.filter(f.ncfile.like("%" + ncfile))
    if start_time is not None:
        q = q.filter(f.time_end >= start_time)
    if end_time is not None:
        q = q.filter(f.time_start <= end_time)
    if frequency is not None:
        q = q.filter(f.frequency == frequency)

    # Attributes that are required to be unique to ensure disambiguation
    for attr, val in attrs_unique.items():
        # If default attribute present and not currently in filter
        # add to attributes filter
        if attr not in attrs:
            if q.filter(v.ncvar_attrs.any(name=attr, value=val)).first():
                attrs.update({attr: val})

    # requested specific attribute values
    for attr, val in attrs.items():
        q = q.filter(v.ncvar_attrs.any(name=attr, value=val))

    ncfiles = q.all()

    if n is not None:
        if n > 0:
            ncfiles = ncfiles[:n]
        else:
            ncfiles = ncfiles[n:]

    # ensure we actually got a result
    if not ncfiles:
        raise VariableNotFoundError(
            "No files were found containing '{}' in the '{}' experiment".format(
                variable, expt
            )
        )

    # check whether the results are unique
    for attr in attrs_unique:
        unique_attributes = set()
        for f in ncfiles:
            if attr in f.NCVar.attrs:
                unique_attributes.add(str(f.NCVar.attrs[attr]))
            else:
                unique_attributes.add(None)
        if len(unique_attributes) > 1:
            warnings.warn(
                f"Your query returns variables from files with different {attr}: {unique_attributes}. "
                "This could lead to unexpected behaviour! Disambiguate by passing "
                f"attrs={{'{attr}'=''}} to getvar, specifying the desired attribute value.",
                QueryWarning,
            )

    unique_freqs = set(f.NCFile.frequency for f in ncfiles)
    if len(unique_freqs) > 1:
        warnings.warn(
            f"Your query returns files with differing frequencies: {unique_freqs}. "
            "This could lead to unexpected behaviour! Disambiguate by passing "
            "frequency= to getvar, specifying the desired frequency.",
            QueryWarning,
        )

    return ncfiles


def _parse_chunks(ncvar):
    """Parse an NCVar, returning a dictionary mapping dimensions to chunking along that dimension."""

    try:
        # this should give either a list, or 'None' (other values will raise an exception)
        var_chunks = eval(ncvar.chunking)
        if var_chunks is not None:
            return dict(zip(eval(ncvar.dimensions), var_chunks))

        return None

    except NameError:
        # chunking could be 'contiguous', which doesn't evaluate
        return None
