import scqubits as scq
import qutip as qt
import numpy as np


class TransmonOscillator:
    def __init__(self, EJ, EC, ng, tmon_charge_cutoff, E_osc, osc_cutoff, tmon_eigen_cutoff):
        self.EJ = EJ
        self.EC = EC
        self.ng = ng
        self.tmon_charge_Cutoff = tmon_charge_cutoff
        self.E_osc = E_osc
        self.osc_cutoff = osc_cutoff
        self.tmon_eigen_cutoff
        self.tmon = scq.Transmon(EJ=EJ, EC=EC, ng=ng, ncut=tmon_charge_cutoff, truncated_dim=tmon_eigen_cutoff)
        self.osc = scq.Oscillator(E_osc=E_osc, truncated_dim=osc_cutoff)


