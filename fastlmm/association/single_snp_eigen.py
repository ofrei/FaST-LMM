import logging
import pandas as pd
import os
from pathlib import Path
import numpy as np
import scipy.stats as stats
import pysnptools.util as pstutil
from unittest.mock import patch
from pysnptools.standardizer import Unit
from pysnptools.snpreader import SnpData
from pysnptools.eigenreader import EigenData
from fastlmm.inference.fastlmm_predictor import (
    _pheno_fixup,
    _snps_fixup,
    _kernel_fixup,
)
from fastlmm.inference import LMM
from fastlmm.util.mingrid import minimize1D

# !!!cmk move to pysnptools
def eigen_from_kernel(K, kernel_standardizer, count_A1=None):
    """!!!cmk documentation"""
    # !!!cmk could offer a low-memory path that uses memmapped files
    from pysnptools.kernelreader import SnpKernel
    from pysnptools.kernelstandardizer import Identity as KS_Identity

    assert K is not None
    K = _kernel_fixup(K, iid_if_none=None, standardizer=Unit(), count_A1=count_A1)
    assert K.iid0 is K.iid1, "Expect K to be square"

    if isinstance(
        K, SnpKernel
    ):  # !!!make eigen creation a method on all kernel readers
        assert isinstance(
            kernel_standardizer, KS_Identity
        ), "cmk need code for other kernel standardizers"
        vectors, sqrt_values, _ = np.linalg.svd(
            K.snpreader.read().standardize(K.standardizer).val, full_matrices=False
        )
        if np.any(sqrt_values < -0.1):
            logging.warning("kernel contains a negative Eigenvalue")
        eigen = EigenData(values=sqrt_values * sqrt_values, vectors=vectors, iid=K.iid)
    else:
        # !!!cmk understand _read_kernel, _read_with_standardizing

        K = K._read_with_standardizing(
            kernel_standardizer=kernel_standardizer,
            to_kerneldata=True,
            return_trained=False,
        )
        # !!! cmk ??? pass in a new argument, the kernel_standardizer(???)
        logging.debug("About to eigh")
        w, v = np.linalg.eigh(K.val)  # !!! cmk do SVD sometimes?
        logging.debug("Done with to eigh")
        if np.any(w < -0.1):
            logging.warning(
                "kernel contains a negative Eigenvalue"
            )  # !!!cmk this shouldn't happen with a RRM, right?
        # !!!cmk remove very small eigenvalues
        # !!!cmk remove very small eigenvalues in a way that doesn't require a memcopy?
        eigen = EigenData(values=w, vectors=v, iid=K.iid)
        # eigen.vectors[:,eigen.values<.0001]=0.0
        # eigen.values[eigen.values<.0001]=0.0
        # eigen = eigen[:,eigen.values >= .0001] # !!!cmk const
    return eigen


# !!!LATER add warning here (and elsewhere) K0 or K1.sid_count < test_snps.sid_count,
#  might be a covar mix up.(but only if a SnpKernel
def single_snp_eigen(
    test_snps,
    pheno,
    eigenreader,
    covar=None,  # !!!cmk covar_by_chrom=None, leave_out_one_chrom=True,
    output_file_name=None,
    log_delta=None,
    # !!!cmk cache_file=None, GB_goal=None, interact_with_snp=None,
    # !!!cmk runner=None, map_reduce_outer=True,
    # !!!cmk pvalue_threshold=None,
    # !!!cmk random_threshold=None,
    # !!!cmk random_seed = 0,
    # min_log_delta=-5,  # !!!cmk make this a range???
    # max_log_delta=10,
    # !!!cmk xp=None,
    fit_log_delta_via_reml=True,
    test_via_reml=False,
    count_A1=None,
):
    """cmk documentation"""
    # !!!LATER raise error if covar has NaN
    # cmk t0 = time.time()

    if output_file_name is not None:
        os.makedirs(Path(output_file_name).parent, exist_ok=True)

    xp = pstutil.array_module("numpy")
    with patch.dict("os.environ", {"ARRAY_MODULE": xp.__name__}) as _:

        assert test_snps is not None, "test_snps must be given as input"
        test_snps = _snps_fixup(test_snps, count_A1=count_A1)
        pheno = _pheno_fixup(pheno, count_A1=count_A1).read()
        good_values_per_iid = (pheno.val == pheno.val).sum(axis=1)
        assert not np.any(
            (good_values_per_iid > 0) * (good_values_per_iid < pheno.sid_count)
        ), "With multiple phenotypes, an individual's values must either be all missing or have no missing."
        # !!!cmk multipheno
        # drop individuals with no good pheno values.
        pheno = pheno[good_values_per_iid > 0, :]
        covar = _pheno_fixup(covar, iid_if_none=pheno.iid, count_A1=count_A1)

        # !!!cmk assert covar_by_chrom is None, "When 'leave_out_one_chrom' is False,
        #  'covar_by_chrom' must be None"
        # !!!cmk fix up w and v
        iid_count_before = eigenreader.iid_count
        test_snps, pheno, eigenreader, covar = pstutil.intersect_apply(
            [test_snps, pheno, eigenreader, covar]
        )
        logging.debug("# of iids now {0}".format(test_snps.iid_count))
        assert (
            eigenreader.iid_count == iid_count_before
        ), "Expect all of eigenreader's individuals to be in test_snps, pheno, and covar."  # cmk ok to lose some?
        # !!!cmk K0, K1, block_size = _set_block_size(K0, K1, mixing, GB_goal,
        #  force_full_rank, force_low_rank)

        # !!! cmk
        # if h2 is not None and not isinstance(h2, np.ndarray):
        #     h2 = np.repeat(h2, pheno.shape[1])

        # view_ok because this code already did a fresh read to look for any
        #  missing values
        eigendata = eigenreader.read(view_ok=True, order="A")

        # ============
        # iid_count x eid_count  *  iid_count x covar => eid_count * covar
        # O(iid_count x eid_count x covar)
        # =============
        covar = _covar_with_bias(covar)
        covar_r = eigendata.rotate(covar)

        assert pheno.sid_count >= 1, "Expect at least one phenotype"
        assert pheno.sid_count == 1, "currently only have code for one pheno"
        # ============
        # iid_count x eid_count  *  iid_count x pheno_count => eid_count * pheno_count
        # O(iid_count x eid_count x pheno_count)
        # =============
        # !!! cmk with multipheno is it going to be O(covar*covar*y)???
        y_r = eigendata.rotate(pheno.read(view_ok=True, order="A"))

        if log_delta is None:
            # cmk As per the paper, we optimized delta with REML=True, but
            # cmk we will later optimize beta and find log likelihood with ML (REML=False)
            h2 = _find_h2(
                eigendata, covar.val, covar_r, y_r, REML=fit_log_delta_via_reml, minH2=0.00001
            )["h2"]
            K = Kthing(eigendata, h2=h2)
        else:
            # !!!cmk internal/external doesn't matter if full rank, right???
            K = Kthing(eigendata, log_delta=log_delta)

        yKy = AKB(y_r, K, y_r)
        covarKcovar = AKB(covar_r, K, covar_r)
        covarKy = AKB(covar_r, K, y_r, aK=covarKcovar.aK)

        if test_via_reml: # !!!cmk
            ll_null, beta = _loglikelihood_reml(covar.val, yKy, covarKcovar, covarKy)
        else:
            ll_null, beta, variance_beta = _loglikelihood_ml(yKy, covarKcovar, covarKy)

        cc = covar_r.sid_count  # number of covariates including bias
        if test_via_reml: # !!!cmk
            X = np.full((covar.iid_count,cc+1),fill_value=np.nan) 
            X[:,:cc] = covar.val # left
        XKX = AKB.empty((cc + 1, cc + 1), K=K)
        XKX[:cc, :cc] = covarKcovar  # upper left

        XKy = AKB.empty((cc + 1, y_r.sid_count), K=K)
        XKy[:cc, :] = covarKy  # upper

        # !!!cmk really do this in batches in different processes
        batch_size = 1000  # !!!cmk const
        result_list = []
        for sid_start in range(0, test_snps.sid_count, batch_size):
            sid_end = np.min([sid_start + batch_size, test_snps.sid_count])

            snps_batch = test_snps[:, sid_start:sid_end].read().standardize()
            # !!!cmk should biobank precompute this?
            alt_batch_r = eigendata.rotate(snps_batch)

            covarKalt_batch = AKB(covar_r, K, alt_batch_r, aK=covarKcovar.aK)
            alt_batchKy = AKB(alt_batch_r, K, y_r)

            for i in range(sid_end - sid_start):
                alt_r = alt_batch_r[i]


                XKX[:cc, cc:] = covarKalt_batch[:, i : i + 1]  # upper right
                XKX[cc:, :cc] = XKX[:cc, cc:].T  # lower left
                XKX[cc:, cc:] = AKB(
                    alt_r, K, alt_r, aK=alt_batchKy.aK[:, i : i + 1]
                )  # lower right

                XKy[cc:, :] = alt_batchKy[i : i + 1, :]  # lower

                # O(sid_count * (covar+1)^6)
                if test_via_reml: # !!!cmk
                    X[:,cc:] = snps_batch.val[:,i:i+1] # right
                    ll_alt, beta = _loglikelihood_reml(X, yKy, XKX, XKy)
                    variance_beta = np.nan
                else:
                    ll_alt, beta, variance_beta = _loglikelihood_ml(yKy, XKX, XKy)

                test_statistic = ll_alt - ll_null
                result_list.append(
                    {
                        "PValue": stats.chi2.sf(2.0 * test_statistic, df=1),
                        "SnpWeight": beta,
                        "SnpWeightSE": np.sqrt(variance_beta),
                    }
                )

        dataframe = _create_dataframe().append(result_list, ignore_index=True)
        dataframe["sid_index"] = range(test_snps.sid_count)
        dataframe["SNP"] = test_snps.sid
        dataframe["Chr"] = test_snps.pos[:, 0]
        dataframe["GenDist"] = test_snps.pos[:, 1]
        dataframe["ChrPos"] = test_snps.pos[:, 2]
        dataframe["Nullh2"] = np.zeros(test_snps.sid_count) + K.h2
        # !!!cmk in lmmcov, but not lmm
        # dataframe['SnpFractVarExpl'] = np.sqrt(fraction_variance_explained_beta[:,0])
        # !!!cmk Feature not supported. could add "0"
        # dataframe['Mixing'] = np.zeros((len(sid))) + 0

    dataframe.sort_values(by="PValue", inplace=True)
    dataframe.index = np.arange(len(dataframe))

    if output_file_name is not None:
        dataframe.to_csv(output_file_name, sep="\t", index=False)

    return dataframe


def _covar_with_bias(covar):
    covar_val0 = covar.read(view_ok=True, order="A").val
    covar_val1 = np.c_[
        covar_val0, np.ones((covar.iid_count, 1))
    ]  # view_ok because np.c_ will allocation new memory
    # !!!cmk what is "bias' is already used as column name
    covar_and_bias = SnpData(
        iid=covar.iid,
        sid=list(covar.sid) + ["bias"],
        val=covar_val1,
        name=f"{covar}&bias",
    )
    return covar_and_bias


# !!!cmk needs better name
class Kthing:
    def __init__(self, eigendata, h2=None, log_delta=None):
        assert (
            sum([h2 is not None, log_delta is not None]) == 1
        ), "Exactly one of h2, etc should have a value"
        if h2 is not None:
            self.h2 = h2
            self.delta = 1.0 / h2 - 1.0
            self.log_delta = np.log(self.delta)
        elif log_delta is not None:
            self.log_delta = log_delta
            self.delta = np.exp(log_delta)
            self.h2 = 1.0 / (self.delta + 1)
        else:
            assert False, "real assert"

        self.iid_count = eigendata.iid_count
        self.is_low_rank = eigendata.is_low_rank
        # "reshape" lets it broadcast
        self.Sd = (eigendata.values + self.delta).reshape(-1, 1)
        self.logdet = np.log(self.Sd).sum()
        if eigendata.is_low_rank:  # !!!cmk test this
            self.logdet += (eigendata.iid_count - eigendata.eid_count) * self.log_delta


# !!!cmk move to PySnpTools
class AKB:
    # !!!cmk document only an unmodified AKV(ar, K, br) will have an aK
    def __init__(self, a_r, K, b_r, aK=None):
        self.K = K
        if aK is None:
            self.aK = a_r.rotated.val / K.Sd
        else:
            self.aK = aK

        self.aKb = self.aK.T.dot(b_r.rotated.val)
        if K.is_low_rank:
            self.aKb += a_r.double.val.T.dot(b_r.double.val) / K.delta

    @staticmethod
    def empty(shape, K):
        result = AKB.__new__(AKB)
        result.K = K
        result.aKb = np.full(shape=shape, fill_value=np.NaN)
        result.aK = None
        return result

    def __setitem__(self, key, value):
        # !!!cmk may want to check that the K's are equal
        self.aKb[key] = value.aKb

    def __getitem__(self, index):
        result = AKB.__new__(AKB)
        result.aKb = self.aKb[index]
        result.aK = None
        result.K = self.K
        return result

    @property
    def T(self):
        result = AKB.__new__(AKB)
        result.aKb = self.aKb.T
        result.aK = None
        result.K = self.K
        return result


def _find_h2(eigendata, X, X_r, y_r, REML, nGridH2=10, minH2 = 0.0, maxH2 = 0.99999):
    # !!!cmk log delta is used here. Might be better to use findH2, but if so will need to normalized G so that its K's diagonal would sum to iid_count
    logging.info("searching for delta/h2/logdelta")

    if False: # !!!cmk
        # !!!cmk expect one pass per y column
        lmm = LMM()
        lmm.S = eigendata.values
        lmm.U = eigendata.vectors
        lmm.UX = X_r.rotated.val
        lmm.UUX = X_r.double.val if X_r.double is not None else None
        lmm.Uy = y_r.rotated.val[:, 0]  # !!!cmk not multipheno
        lmm.UUy = y_r.double.val[:, 0] if y_r.double is not None else None

        if REML:
            lmm.X = X

        result_0 = lmm.findH2(REML=REML, minH2=0.00001)
    if True:
        resmin=[None]
        def f(x,resmin=resmin,**kwargs):
            K = Kthing(eigendata, h2=x)
            yKy = AKB(y_r, K, y_r)
            XKX = AKB(X_r, K, X_r)
            XKy = AKB(X_r, K, y_r, aK=XKX.aK)

            if REML: # !!!cmk
                nLL, _ = _loglikelihood_reml(X, yKy, XKX, XKy)
            else:
                nLL, _, _ = _loglikelihood_ml(yKy, XKX, XKy)
            nLL = -nLL # !!!cmk
            if (resmin[0] is None) or (nLL<resmin[0]["nLL"]):
                resmin[0]={"nLL":nLL,"h2":x}
            logging.debug(f"search\t{x}\t{nLL}")
            return nLL
        min = minimize1D(f=f, nGrid=nGridH2, minval=0.00001, maxval=maxH2 )
        result_1 = resmin[0]
        # !!! cmk logging.debug(f"{result_1},{result_0}")
        return result_1

def _loglikelihood_reml(X, yKy, XKX, XKy):
    K = yKy.K  # !!!cmk may want to check that all three K's are equal
    # !!!cmk similar code in _loglikelihood_ml
    # !!!cmk may want to check that all three K's are equal
    yKy = float(yKy.aKb)  # !!!cmk assuming one pheno
    XKX = XKX.aKb
    XKy = XKy.aKb.reshape(-1)  # cmk should be 2-D to support multiple phenos

    # Must do one test at a time
    SxKx, UxKx = np.linalg.eigh(XKX)
    # Remove tiny eigenvectors
    i_pos = SxKx > 1e-10
    UxKx = UxKx[:, i_pos]
    SxKx = SxKx[i_pos]

    beta = UxKx.dot(UxKx.T.dot(XKy) / SxKx)
    r2 = yKy - XKy.dot(beta)

    XX = X.T.dot(X)
    [Sxx,Uxx]= np.linalg.eigh(XX)
    logdetXX  = np.log(Sxx).sum()
    logdetXKX = np.log(SxKx).sum()
    sigma2 = r2 / (X.shape[0]-X.shape[1])
    nLL =  0.5 * ( K.logdet + logdetXKX - logdetXX + (X.shape[0]-X.shape[1]) * ( np.log(2.0*np.pi*sigma2) + 1 ) )
    assert np.all(
        np.isreal(nLL)
    ), "nLL has an imaginary component, possibly due to constant covariates"
    # !!!cmk which is negative loglikelihood and which is LL?
    return -nLL, beta


def _loglikelihood_ml(yKy, XKX, XKy):
    K = yKy.K  # !!!cmk may want to check that all three K's are equal
    yKy = float(yKy.aKb)  # !!!cmk assuming one pheno
    XKX = XKX.aKb
    XKy = XKy.aKb.reshape(-1)  # cmk should be 2-D to support multiple phenos

    # Must do one test at a time
    SxKx, UxKx = np.linalg.eigh(XKX)
    # Remove tiny eigenvectors
    i_pos = SxKx > 1e-10
    UxKx = UxKx[:, i_pos]
    SxKx = SxKx[i_pos]

    beta = UxKx.dot(UxKx.T.dot(XKy) / SxKx)
    r2 = yKy - XKy.dot(beta)
    sigma2 = r2 / K.iid_count
    nLL = 0.5 * (K.logdet + K.iid_count * (np.log(2.0 * np.pi * sigma2) + 1))
    assert np.all(
        np.isreal(nLL)
    ), "nLL has an imaginary component, possibly due to constant covariates"
    variance_beta = K.h2 * sigma2 * (UxKx / SxKx * UxKx).sum(-1)
    # !!!cmk which is negative loglikelihood and which is LL?
    return -nLL, beta, variance_beta


# !!!cmk similar to single_snp.py and single_snp_scale
def _create_dataframe():
    # https://stackoverflow.com/questions/21197774/assign-pandas-dataframe-column-dtypes
    dataframe = pd.DataFrame(
        np.empty(
            (0,),
            dtype=[
                ("sid_index", np.float),
                ("SNP", "S"),
                ("Chr", np.float),
                ("GenDist", np.float),
                ("ChrPos", np.float),
                ("PValue", np.float),
                ("SnpWeight", np.float),
                ("SnpWeightSE", np.float),
                ("SnpFractVarExpl", np.float),
                ("Mixing", np.float),
                ("Nullh2", np.float),
            ],
        )
    )
    return dataframe
