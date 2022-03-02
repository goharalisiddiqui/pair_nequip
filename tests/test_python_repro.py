import pytest

import os
import tempfile
import subprocess
from pathlib import Path
import numpy as np
import yaml
import textwrap
from io import StringIO

import ase
import ase.build
import ase.io

import torch

from nequip.ase import NequIPCalculator
from nequip.utils import Config
from nequip.data import dataset_from_config, AtomicData, AtomicDataDict

TESTS_DIR = Path(__file__).resolve().parent


# TODO: add a tiny cell with a giant cutoff for self images
@pytest.fixture(
    params=[
        # ("aspirin.xyz", "aspirin", ["C", "H", "O"], 4.0, {}),
        # ("aspirin.xyz", "aspirin", ["C", "H", "O"], 15.0, {}),
        # ("w-14-subset.xyz", "w-14", ["W"], 4.5, {}),
        (
            "w-14-subset-two-atom.xyz",
            "w-14-twoatom",
            ["W"],
            3.0,
            {"per_species_rescale_scales": "dataset_per_atom_total_energy_std"},
        ),
        (
            "w-14-subset-two-atom.xyz",
            "w-14-twoatom",
            ["W"],
            6.0,
            {"per_species_rescale_scales": "dataset_per_atom_total_energy_std"},
        ),
    ]
)
def dataset_options(request):
    out = dict(
        zip(
            ["dataset_file_name", "run_name", "chemical_symbols", "r_max"],
            request.param,
        )
    )
    out["dataset_file_name"] = TESTS_DIR / ("test_data/" + out["dataset_file_name"])
    out.update(request.param[-1])
    return out


@pytest.fixture(params=[187382, 109109])
def model_seed(request):
    return request.param


@pytest.fixture()
def deployed_model(model_seed, dataset_options):
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config.from_file(str(TESTS_DIR / "test_data/test_repro.yaml"))
        config.update(dataset_options)
        config["seed"] = model_seed
        config["root"] = tmpdir + "/root"
        configpath = tmpdir + "/config.yaml"
        with open(configpath, "w") as f:
            yaml.dump(dict(config), f)
        # run a nequip-train command
        retcode = subprocess.run(
            ["nequip-train", configpath],
            cwd=tmpdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        retcode.check_returncode()
        # run nequip-deploy
        deployed_path = tmpdir + "/deployed.pth"
        retcode = subprocess.run(
            [
                "nequip-deploy",
                "build",
                config["root"] + "/" + config["run_name"],
                deployed_path,
            ],
            cwd=tmpdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        retcode.check_returncode()
        # load structures to test on
        d = dataset_from_config(config)
        # take some frames
        structures = [d[i].to_ase(type_mapper=d.type_mapper) for i in range(5)]
        # give them cells even if nonperiodic
        if not all(structures[0].pbc):
            L = 50.0
            for struct in structures:
                struct.cell = L * np.eye(3)
                struct.center()
        yield deployed_path, structures, config


def test_repro(deployed_model):
    structure: ase.Atoms
    deployed_model: str
    deployed_model, structures, config = deployed_model
    num_types = len(config["chemical_symbols"])

    calc = NequIPCalculator.from_deployed_model(
        deployed_model,
        set_global_options=True,
        species_to_type_name={s: s for s in config["chemical_symbols"]},
    )

    newline = "\n"
    periodic = all(structures[0].pbc)
    lmp_in = textwrap.dedent(
        f"""units		metal
        atom_style	atomic
        newton off
        thermo 1

        # get a box defined before pair_coeff
        {'boundary p p p' if periodic else 'boundary s s s'}

        read_data structure0.data


        pair_style	nequip
        # note that ASE outputs lammps types in alphabetical order of chemical symbols
        # since we use chem symbols in this test, just put the same
        pair_coeff	* * {deployed_model} {' '.join(sorted(set(config["chemical_symbols"])))}
        {newline.join('mass  %i 1.0' % i for i in range(1, num_types + 1))}

        neighbor	1.0 bin
        neigh_modify    delay 0 every 1

        fix		1 all nve

        timestep	0.001

        compute atomicenergies all pe/atom
        compute totalatomicenergy all reduce sum c_atomicenergies

        thermo_style custom step time temp pe c_totalatomicenergy etotal press spcpu cpuremain
        """
    )
    PRECISION_CONST: float = 1000.0
    for i in range(len(structures)):
        lmp_in += textwrap.dedent(
            f"""delete_atoms group all
            read_data structure{i}.data add merge
            run 0
            print $({PRECISION_CONST} * pe) file pe{i}.dat
            print $({PRECISION_CONST} * c_totalatomicenergy) file totalatomicenergy{i}.dat
            write_dump all custom output{i}.dump id type x y z fx fy fz c_atomicenergies modify format float %20.15g
            """
        )

    # for each model,structure pair
    # build a LAMMPS input using that structure
    with tempfile.TemporaryDirectory() as tmpdir:
        # save out the structure
        for i, structure in enumerate(structures):
            ase.io.write(
                tmpdir + f"/structure{i}.data", structure, format="lammps-data"
            )
        # save out the LAMMPS input:
        infile_path = tmpdir + "/test_repro.in"
        with open(infile_path, "w") as f:
            f.write(lmp_in)
        # environment variables
        env = dict(os.environ)
        env["NEQUIP_DEBUG"] = "true"

        # run LAMMPS
        # TODO: use NEQUIP_DEBUG env var to get input printouts and compare
        retcode = subprocess.run(
            [env.get("LAMMPS", "lmp"), "-in", infile_path],
            cwd=tmpdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        retcode.check_returncode()

        # load debug data:
        model_inputs = []
        lammps_stdout = iter(retcode.stdout.decode("utf-8").splitlines())
        line = next(lammps_stdout, None)
        while line is not None:
            if line.startswith("NEQUIP edges: i j xi[:] xj[:] cell_shift[:] rij"):
                edges = []
                while True:
                    line = next(lammps_stdout)
                    if line.startswith("end NEQUIP edges"):
                        break
                    edges.append(line)
                edges = np.loadtxt(StringIO("\n".join(edges)))
                model_inputs.append(edges)
            line = next(lammps_stdout, None)
        new_model_inputs = []
        for mi in model_inputs:
            new_model_inputs.append(
                {
                    "i": mi[:, 0:1].astype(int),
                    "j": mi[:, 1:2].astype(int),
                    "cell_shift": mi[:, 8:11].astype(int),
                }
            )
        model_inputs = new_model_inputs
        del new_model_inputs

        # load dumped data
        for i, structure in enumerate(structures):
            # first, check the model INPUTS
            structure_data = AtomicData.from_ase(
                structure, r_max=float(config["r_max"])
            )
            mi = model_inputs[i]
            lammps_edge_tuples = set(
                tuple(e) for e in np.hstack((mi["i"], mi["j"], mi["cell_shift"]))
            )
            nq_edge_tuples = set(
                tuple(e.tolist())
                for e in torch.hstack(
                    (
                        structure_data[AtomicDataDict.EDGE_INDEX_KEY].t(),
                        structure_data[AtomicDataDict.EDGE_CELL_SHIFT_KEY].to(
                            torch.long
                        ),
                    )
                )
            )
            # edge i,j,shift tuples should be unique
            assert len(lammps_edge_tuples) == len(mi["i"])
            assert lammps_edge_tuples == nq_edge_tuples

            # now check the OUTPUTS
            structure.calc = calc
            lammps_result = ase.io.read(
                tmpdir + f"/output{i}.dump", format="lammps-dump-text"
            )

            # check output atomic quantities
            assert np.allclose(
                structure.get_forces(),
                lammps_result.get_forces(),
                atol=1e-5,
            )
            assert np.allclose(
                structure.get_potential_energies(),
                lammps_result.arrays["c_atomicenergies"].reshape(-1),
                atol=1e-6,
            )

            # check system quantities
            lammps_pe = (
                float(Path(tmpdir + f"/pe{i}.dat").read_text()) / PRECISION_CONST
            )
            lammps_totalatomicenergy = (
                float(Path(tmpdir + f"/totalatomicenergy{i}.dat").read_text())
                / PRECISION_CONST
            )
            assert np.allclose(lammps_pe, lammps_totalatomicenergy)
            assert np.allclose(
                structure.get_potential_energy(),
                lammps_pe,
                atol=1e-6,
            )
