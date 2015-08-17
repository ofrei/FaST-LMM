import numpy as np
import logging
import unittest
import os
from fastlmm.feature_selection import FeatureSelectionStrategy, load_snp_data
from pysnptools.snpreader import Bed,Pheno
from pysnptools.kernelreader import SnpKernel
from pysnptools.kernelreader import Identity as KernelIdentity
import pysnptools.util as pstutil
from fastlmm.feature_selection.feature_selection_two_kernel import FeatureSelectionInSample
from fastlmm.association import single_snp
from pysnptools.standardizer import DiagKtoN,UnitTrained
from fastlmm.inference.lmm import LMM
from pysnptools.util import intersect_apply
from pysnptools.snpreader import SnpData,SnpReader
from pysnptools.standardizer import Unit

def _snps_fixup(snp_input, iid_if_none=None):
    if isinstance(snp_input, str):
        return Bed(snp_input)
    if snp_input is None:
        assert iid_if_none is not None, "snp_input cannot be None here"
        return SnpData(iid_if_none, sid=np.empty((0),dtype='str'), val=np.empty((len(iid_if_none),0)),pos=np.empty((0,3)),parent_string="") #!!!make a static factory method on SnpData

    return snp_input

def _pheno_fixup(pheno_input, iid_if_none=None, missing ='-9'):

    try:
        ret = Pheno(pheno_input, iid_if_none)
        ret.iid #doing this just to force file load
        return ret
    except:
        return _snps_fixup(pheno_input, iid_if_none=iid_if_none)


    return pheno_input

def _kernel_fixup(input, iid_if_none, standardizer, test=None, test_iid_if_none=None):
    if test is not None and input is None:
        input = test
        test = None

    if isinstance(input, str) and input.endswith(".npz"):
        return KernelNpz(input)

    if isinstance(input, str):
        input = Bed(input)
    if isinstance(test, str):
        test = Bed(test)

    if isinstance(input,SnpReader):
        return SnpKernel(input,standardizer=standardizer,test=test)

    if input is None:
        return KernelIdentity(iid=iid_if_none,test=test_iid_if_none)

    return input


class FastLmmModel(object):

    def __init__(self):
        pass

    @staticmethod
    def new_snp_name(snpreader):
        new_snp = "always1"
        while True:
            if not new_snp in snpreader.sid:
                return np.r_[snpreader.sid,[new_snp]]
            new_snp += "_"
    
    @staticmethod
    def learn(K0_train=None, covar_train=None, pheno_train=None, h2=None):
        #!!!cmk add documentation including that h2 is how much weight to give to K0 vs identity matrix



        assert pheno_train is not None, "pheno must be given"

        pheno_train = _pheno_fixup(pheno_train)
        assert pheno_train.sid_count == 1, "Expect pheno to be just one variable"
        covar_train = _pheno_fixup(covar_train, iid_if_none=pheno_train.iid)

        #!!!cmk delete this line (not the next): if K0_train is not None: #If K0_train is None, we leave it
        K0_train = _kernel_fixup(K0_train, iid_if_none=pheno_train.iid, standardizer=Unit()) #!!!cmk if K0_train is None we could set it to KernelIdentity or we could make it SNPs with zero width

        K0_train, covar_train, pheno_train  = intersect_apply([K0_train, covar_train, pheno_train],intersect_before_standardize=True) #!!!cmk check that 'True' is what we want

        covar_train = covar_train.read()
        # If possible, unit standardize train and test together. If that is not possible, unit standardize only train and later apply
        # the same linear transformation to test. Unit standardization is necessary for FastLMM to work correctly.
        #!!!cmk is the calculation of the training data's stats done twice???
        covar_unit_trained = Unit()._train_standardizer(covar_train,apply_in_place=True) #This also fills missing with the mean #!!!cmk right?

        # add a column of 1's to cov to increase DOF of model (and accuracy) by allowing a constant offset
        covar_train = SnpData(iid=covar_train.iid,
                              sid=FastLmmModel.new_snp_name(covar_train),
                              val=np.c_[covar_train.val,np.ones((covar_train.iid_count,1))])

        # do final prediction using lmm.py
        lmm = LMM()

        #Special case: The K0 kernel is defined implicitly with SNP data
        if isinstance(K0_train, SnpKernel): #!!!cmk would it be more pythonisque to use a try/catch?
            assert isinstance(K0_train.standardizer,Unit), "Expect Unit standardizer"
            G0_train = K0_train.snpreader.read()

            #!!!cmk we remember this
            G0_unit_trained = Unit()._train_standardizer(G0_train,apply_in_place=True) #This also fills missing with the mean

            ## Scale G_train so that its K will have a diagonal of iid_count. While not absolutely required, this scaling
            ## improves the search for the best h2. If G_test is available, scale the two parts together. If not,
            ## find the scale factor on just train and later apply it to test.
            if G0_train.sid_count == 0:
                factor = 1
            else:
                vec = G0_train.val.reshape(-1, order="A") #!!!cmk would be nice to not have this code in two places
                ## make sure no copy was made
                assert not vec.flags['OWNDATA'] #!!!cmk add trained version of diag.. inside pysnptools
                squared_sum = vec.dot(vec)
                factor = 1./(squared_sum / float(G0_train.iid_count))
                G0_train.val *= np.sqrt(factor)
            G0_train_sid = G0_train.sid
            lmm.setG(G0=G0_train.val)
        #elif K0_train is None: #!!!cmk remove this section
        #    factor = None
        #    lmm.setG(K0=np.zeros([0,0]))
        #    G0_unit_trained = None
        #    G0_train_sid = None
        else:
            #when K0_train is None or Identity should we use setG with None?
            #!!!cmk Use standardize, but remember it
            K0_train = K0_train.read()#.standardize() #!!!cmk block_size??? 
            factor = float(K0_train.iid_count) / np.diag(K0_train.val).sum()
            if abs(factor-1.0)>1e-15:
                K0_train.val *= factor
            lmm.setK(K0=K0_train.val)
            G0_unit_trained = None
            G0_train_sid = None

        lmm.setX(covar_train.val)
        pheno_train = pheno_train.read()
        lmm.sety(pheno_train.val[:,0])

        # Find the best h2 and also on covariates (not given from new model)
        if h2 is None:
            res = lmm.findH2() #!!!why is REML true in the return???
        else:
            res = lmm.nLLeval(h2=h2)


        #We compute sigma2 instead of using res['sigma2'] because res['sigma2'] is only the pure noise.
        full_sigma2 = float(sum((np.dot(covar_train.val,res['beta']).reshape(-1,1)-pheno_train.val)**2))/pheno_train.iid_count #!!!cmk this is non REML. Is that right?

        ###### all references to 'fastlmm_model' should be here so that we don't forget any
        fastlmm_model = FastLmmModel()
        fastlmm_model.beta = res['beta']
        fastlmm_model.h2 = res['h2']
        fastlmm_model.sigma2 = full_sigma2
        fastlmm_model.U = lmm.U
        fastlmm_model.S = lmm.S
        fastlmm_model.K = lmm.K
        fastlmm_model.G = lmm.G
        fastlmm_model.y = lmm.y
        fastlmm_model.Uy = lmm.Uy
        fastlmm_model.X = lmm.X
        fastlmm_model.UX = lmm.UX
        fastlmm_model.factor = factor
        fastlmm_model.G0_unit_trained = G0_unit_trained
        fastlmm_model.covar_unit_trained = covar_unit_trained
        fastlmm_model.K0_train_iid = K0_train.iid
        fastlmm_model.G0_sid = G0_train_sid
        fastlmm_model.covar_sid = covar_train.sid
        fastlmm_model.pheno_sid = pheno_train.sid

        return fastlmm_model

    #!!!cmk need to test on both low-rank and full-rank
    def predict(self,K0_test=None,K0_test_test=None,covar_test=None): #!!!cmk K0_test_test need a _fixup, etc and testing
        #!!!cmk ask david to confirm that G's must have zero mean, but not 1 std (because the diag changes that)
        #!!!have a way so that test & train snps can be optionally standardized together
        #!!!have an option for a 2nd cov and to search (or set a2)

        assert K0_test is not None or covar_test is not None, "Cannot have both K0_test and covar_test as None (but either can have zero features)"

        assert (K0_test is None) == (K0_test_test is None), "K0_test_test should be given exactly when K0_test is given"

        if K0_test is None:
            covar_test = _pheno_fixup(covar_test)
            K0_test = _kernel_fixup(None, iid_if_none=self.K0_train_iid, standardizer=self.G0_unit_trained, test=K0_test, test_iid_if_none=covar_test.iid)
        else:
            K0_test = _kernel_fixup(None, iid_if_none=None, standardizer=self.G0_unit_trained, test=K0_test, test_iid_if_none=None)
            covar_test = _pheno_fixup(covar_test,iid_if_none=K0_test.iid1)
        K0_test_test = _kernel_fixup(K0_test_test, iid_if_none=covar_test.iid, standardizer=self.G0_unit_trained)

        K0_test, covar_test,K0_test_test  = intersect_apply([K0_test, covar_test,K0_test_test],intersect_before_standardize=True,is_test=True) #!!!cmk check that 'True' is what we want

        covar_test = covar_test.read().standardize(self.covar_unit_trained)

        # add a column of 1's to cov to increase DOF of model (and accuracy) by allowing a constant offset
        covar_test = SnpData(iid=covar_test.iid,
                              sid=FastLmmModel.new_snp_name(covar_test),
                              val=np.c_[covar_test.read().val,np.ones((covar_test.iid_count,1))])
        assert np.array_equal(covar_test.sid,self.covar_sid), "Expect covar sids to be the same in train and test."

        lmm = LMM()
        lmm.U = self.U
        lmm.S = self.S
        lmm.G = self.G
        lmm.y = self.y
        lmm.Uy = self.Uy
        lmm.X = self.X
        lmm.UX = self.UX

        if isinstance(K0_test, SnpKernel): #!!!cmk would it be more pythonisque to use a try/catch?
            assert isinstance(K0_test.standardizer,Unit) or isinstance(K0_test.standardizer,UnitTrained), "Expect Unit or UnitTrained standardardizer"
            G0_test = K0_test.test.read().standardize(self.G0_unit_trained)
            if abs(self.factor-1.0)>1e-15:
                G0_test.val *= np.sqrt(self.factor)
                K0_test_test = K0_test_test.read() #!!!cmk what if in snp space?
                K0_test_test.val *= self.factor
            assert np.array_equal(G0_test.sid,self.G0_sid), "Expect G0 sids to be the same in train and test."
            lmm.setTestData(Xstar=covar_test.val, G0star=G0_test.val)
        else:
            K0_test = K0_test.read()#!!!cmk .standardize() #!!!cmk block_size???
            if abs(self.factor-1.0)>1e-15:
                K0_test.val *= self.factor
                K0_test_test = K0_test_test.read() #!!!cmk what if in snp space?
                K0_test_test.val *= self.factor
            lmm.setTestData(Xstar=covar_test.val, K0star=K0_test.val.T)

        pheno_predicted, covar = lmm.predict_mean_and_variance(beta=self.beta, h2=self.h2,sigma2=self.sigma2, Kstar_star=K0_test_test.read().val)

        #pheno_predicted = lmm.predictMean(beta=self.beta, h2=self.h2,scale=self.sigma2).reshape(-1,1)
        ret0 = SnpData(iid = covar_test.iid, sid=self.pheno_sid,val=pheno_predicted,pos=np.array([[np.nan,np.nan,np.nan]]),parent_string="FastLmmModel Prediction")

        #!!!cmk what if everything done in G0 space?
        #covar = lmm.predictVariance(sigma2=self.sigma2, h2=self.h2, Kstar_star=K0_test_test.read().val)
        from pysnptools.kernelreader import KernelData
        ret1 = KernelData(iid=K0_test_test.iid,val=covar)
        return ret0, ret1

    def stats(self):
        sstot = ((self.y-self.y.mean())**2).sum()
        res2 = (self.y-np.dot(self.X,self.beta))**2
        ssres=res2.sum()
        sigma2total = res2.mean()
        sigma2g = sigma2total * self.h2
        sigma2e = sigma2total * (1-self.h2)
        
        r2 = 1.-ssres/sstot
        ret = {"r2":r2,'sigma2total':sigma2total,'sigma2g':sigma2g,'sigma2e':sigma2e,'h2':self.h2,'e2':1-self.h2}
        return ret

    def save(self,filename):
        np.savez(filename,
                 beta=self.beta,
                 sigma2=self.sigma2,
                 h2=self.h2,
                 U=self.U,
                 S=self.S,
                 K=self.K,
                 G=self.G,
                 y=self.y,
                 Uy=self.Uy,
                 X=self.X,
                 UX=self.UX,
                 factor=self.factor,
                 G0_unit_trained_stats=self.G0_unit_trained.stats if self.G is not None else None,
                 covar_unit_trained_stats=self.covar_unit_trained.stats,
                 K0_train_iid = self.K0_train_iid,
                 G0_sid=self.G0_sid,
                 covar_sid=self.covar_sid,
                 pheno_sid=self.pheno_sid
                 )
    @staticmethod
    def load(filename):
        with np.load(filename) as data:
            fastlmm_model = FastLmmModel()
            fastlmm_model.beta=data['beta']
            fastlmm_model.sigma2=data['sigma2']
            fastlmm_model.h2=float(data['h2'])
            fastlmm_model.U=data['U']
            fastlmm_model.S=data['S']
            fastlmm_model.K=data['K']
            fastlmm_model.G=data['G']
            if fastlmm_model.G.shape is ():
                fastlmm_model.G = None
            fastlmm_model.y=data['y']
            fastlmm_model.Uy=data['Uy']
            fastlmm_model.X=data['X']
            fastlmm_model.UX=data['UX']
            fastlmm_model.factor=float(data['factor'])
            fastlmm_model.G0_unit_trained=UnitTrained(stats=data['G0_unit_trained_stats']) if fastlmm_model.G is not None else None
            fastlmm_model.covar_unit_trained=UnitTrained(stats=data['covar_unit_trained_stats'])
            fastlmm_model.K0_train_iid=data['K0_train_iid']
            fastlmm_model.G0_sid=data['G0_sid']
            if fastlmm_model.G0_sid.shape is ():
                fastlmm_model.G0_sid = None
            fastlmm_model.covar_sid=data['covar_sid']
            fastlmm_model.pheno_sid=data['pheno_sid']
            return fastlmm_model

#!!!cmk document
#!!!cmk move to own file
#!!!cmk make FastLmmModel use this when there are no SNPs or K is Identity
#!!!cmk as with FastLmmModel change api to use __init__ instead of learn
class LinearRegressionModel(object):
    def __init__(self):
        pass

    @staticmethod
    def learn(K0_train=None, covar_train=None, pheno_train=None, h2=None):
        assert K0_train is None #!!!cmk could also check that ID or no snps

        covar_train, pheno_train  = intersect_apply([covar_train, pheno_train])
        pheno_train = pheno_train.read() #!!!cmk sharing is OK because we don't change it
        covar_train = covar_train.read() #!!!cmk sharing is OK because we don't change it
        covar_unit_trained = Unit()._train_standardizer(covar_train,apply_in_place=True) #This also fills missing with the mean #!!!cmk right?

        # add a column of 1's to cov to increase DOF of model (and accuracy) by allowing a constant offset
        covar_train = SnpData(iid=covar_train.iid,
                              sid=FastLmmModel.new_snp_name(covar_train),
                              val=np.c_[covar_train.val,np.ones((covar_train.iid_count,1))])


        lsqSol = np.linalg.lstsq(covar_train.val, pheno_train.val[:,0])
        bs=lsqSol[0] #weights
        r2=lsqSol[1] #squared residuals
        D=lsqSol[2]  #rank of design matrix
        N=pheno_train.iid_count

        linear_regression_model = LinearRegressionModel()
        linear_regression_model.beta = bs
        linear_regression_model.ssres = float(r2)
        linear_regression_model.sstot = ((pheno_train.val-pheno_train.val.mean())**2).sum()
        linear_regression_model.covar_unit_trained = covar_unit_trained
        linear_regression_model.iid_count = covar_train.iid_count
        linear_regression_model.covar_sid = covar_train.sid
        linear_regression_model.pheno_sid = pheno_train.sid
        return linear_regression_model

    def predict(self,K0_test=None,K0_test_test=None,covar_test=None): #!!!cmk K0_test_test need a _fixup, etc and testing
        assert K0_test is None #!!!could also be Identity or no snps
        assert K0_test_test is None #!!!could also be Identity or no snps
        assert covar_test is not None, "covar_test is required"

        covar_test = _pheno_fixup(covar_test)
        covar_test = covar_test.read().standardize(self.covar_unit_trained)

        # add a column of 1's to cov to increase DOF of model (and accuracy) by allowing a constant offset
        covar_test = SnpData(iid=covar_test.iid,
                              sid=FastLmmModel.new_snp_name(covar_test),
                              val=np.c_[covar_test.read().val,np.ones((covar_test.iid_count,1))])
        assert np.array_equal(covar_test.sid,self.covar_sid), "Expect covar sids to be the same in train and test."

        pheno_predicted = covar_test.val.dot(self.beta).reshape(-1,1)
        ret0 = SnpData(iid = covar_test.iid, sid=self.pheno_sid,val=pheno_predicted,pos=np.array([[np.nan,np.nan,np.nan]]),parent_string="FastLmmModel Prediction") #!!!replace 'parent_string' with 'name'

        from pysnptools.kernelreader import KernelData
        ret1 = KernelData(iid=covar_test.iid,val=np.eye(covar_test.iid_count)* self.ssres / self.iid_count)
        return ret0, ret1

    def stats(self):
        sigma2total = self.ssres / self.iid_count
        r2 = 1.-self.ssres/self.sstot
        ret = {"r2":r2,'sigma2total':sigma2total,'sigma2g':0,'sigma2e':sigma2total,'h2':0,'e2':1}
        return ret

    def save(self,filename):
        np.savez(filename,
                 beta=self.beta,
                 ssres=self.ssres,
                 sstot=self.sstot,
                 covar_unit_trained_stats=self.covar_unit_trained.stats,
                 iid_count=self.iid_count,
                 covar_sid=self.covar_sid,
                 pheno_sid=self.pheno_sid
                 )
    @staticmethod
    def load(filename):
        with np.load(filename) as data:
            model = LinearRegressionModel()
            model.beta=data['beta']
            model.ssres=data['ssres']
            model.sstot=data['sstot']
            model.covar_unit_trained=UnitTrained(stats=data['covar_unit_trained_stats'])
            model.iid_count=data['iid_count']
            model.covar_sid=data['covar_sid']
            model.pheno_sid=data['pheno_sid']
            return model

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)

    #!!!cmk delete all this and call testing instead


    ## do everything with K0 instead of G0
    ####################################################################
    #snpreader_wholex = Bed("../../tests/datasets/synth/all")
    #covariate_whole = Pheno("../../tests/datasets/synth/cov.txt") #!!!cmk be sure this file is in source control
    #pheno_whole = Pheno("../../tests/datasets/synth/pheno_10_causals.txt")

    #train_idx = np.r_[10:covariate_whole.iid_count] # iids 10 and on
    #test_idx  = np.r_[0:10] # the first 10 iids

    #K0_train = snpreader_wholex[train_idx,:]
    #covar_train = covariate_whole[train_idx,:]
    #pheno_train = pheno_whole[train_idx,:]

    #fastlmm_model1 = FastLmmModel.learn(K0_train, covar_train, pheno_train)

    ##!!!cmk get this working
    #fastlmm_model2 = fastlmm_model1
    ##filename = "tempdir/model1.flm.npz"
    ##pstutil.create_directory_if_necessary(filename)
    ##fastlmm_model1.save(filename)
    ##fastlmm_model2 = FastLmmModel.load(filename)
                
    ## predict on test set
    #K0_test = snpreader_whole[train_idx,:].read_kernel(standardizer=Unit(),test=snpreader_whole[test_idx,:])
    #covar_test = covariate_whole[test_idx,:]

    #predicted_pheno = fastlmm_model2.predict(K0_test, covar_test)

    #pheno_actual = pheno_whole[test_idx,:].read().val[:,0]


    #pylab.plot(pheno_actual, predicted_pheno.val,".")
    #pylab.show()




    ###################################################################
    snpreader_whole = Bed("../../tests/datasets/synth/all")
    covariate_whole = Pheno("../../tests/datasets/synth/cov.txt") #!!!cmk be sure this file is in source control
    pheno_whole = Pheno("../../tests/datasets/synth/pheno_10_causals.txt")

    train_idx = np.r_[10:snpreader_whole.iid_count] # iids 10 and on
    test_idx  = np.r_[0:10] # the first 10 iids

    G0_train = snpreader_whole[train_idx,:]
    covar_train = covariate_whole[train_idx,:]
    pheno_train = pheno_whole[train_idx,:]

    fastlmm_model1 = FastLmmModel.learn(G0_train, covar_train, pheno_train)
    filename = "tempdir/model1.flm.npz"
    pstutil.create_directory_if_necessary(filename)
    fastlmm_model1.save(filename)
    fastlmm_model2 = FastLmmModel.load(filename)
                
    # predict on test set
    G0_test = snpreader_whole[test_idx,:]
    covar_test = covariate_whole[test_idx,:]

    predicted_pheno = fastlmm_model2.predict(G0_test, covar_test)

    pheno_actual = pheno_whole[test_idx,:].read().val[:,0]


    #pylab.plot(pheno_actual, predicted_pheno.val,".")
    #pylab.show()

    #!!!cmk delete this section
    #Prove that trained_unit does the right thing
    covar_train3 = covariate_whole[train_idx,:].read()
    covar_train3.val = np.array([[float(num)] for num in xrange(covar_train3.iid_count)])
    logging.info((covar_train3.val.mean(), covar_train3.val.std())) #should be big
    from pysnptools.standardizer import Unit
    trained = Unit()._train_standardizer(covar_train3,apply_in_place=True)
    logging.info((covar_train3.val.mean(), covar_train3.val.std())) #should be 0,1

    covar_train3 = covariate_whole[train_idx,:].read()
    covar_train3.val = np.array([[float(num)] for num in xrange(covar_train3.iid_count)])
    logging.info((covar_train3.val.mean(), covar_train3.val.std())) #should be big
    covar_train3 = covar_train3.standardize(trained)
    logging.info((covar_train3.val.mean(), covar_train3.val.std())) #should be 0,1
    stats = trained.stats

    covar_train3 = covariate_whole[train_idx,:].read()
    covar_train3.val = np.array([[float(num)] for num in xrange(covar_train3.iid_count)])
    logging.info((covar_train3.val.mean(), covar_train3.val.std())) #should be big
    covar_train3 = covar_train3.standardize(UnitTrained(stats))
    logging.info((covar_train3.val.mean(), covar_train3.val.std())) #should be 0,1



    # Show it doing logistic regression
    covar_train3 = covariate_whole[train_idx,:].read()
    covar_train3.val = np.array([[float(num)] for num in xrange(covar_train3.iid_count)])
    pheno_train3 = pheno_whole[train_idx,:].read()
    np.random.seed(0)
    pheno_train3.val = covar_train3.val * 2.0 + 100 + np.random.normal(size=covar_train3.val.shape) # y = 2*x+100+normal(0,1)

    #pylab.plot(covar_train3.val, pheno_train3.val,".")
    #pylab.show()

    fastlmm_model3 = FastLmmModel.learn(G0_train=G0_train, covar_train=covar_train3, pheno_train=pheno_train3)

    predicted_pheno = fastlmm_model3.predict(G0_test=G0_train, covar_test=covar_train3) #test on train
    #pylab.plot(covar_train3.val, pheno_train3.val,covar_train3.val,predicted_pheno.val,".")
    #pylab.show()
    pheno_actual = pheno_train3.val[:,0]
    #pylab.plot(pheno_actual,predicted_pheno.val,".")
    #pylab.show()


    # Show it using the snps
    pheno_train3 = pheno_whole[train_idx,:].read()
    pheno_train3.val = G0_train[:,0:1].read().val*2

    #pylab.plot(G0_train[:,0:1].read().val[:,0], pheno_train3.val[:,0],".")
    #pylab.show()

    fastlmm_model3 = FastLmmModel.learn(G0_train=G0_train,pheno_train=pheno_train3)

    predicted_pheno = fastlmm_model3.predict(G0_test=G0_train)
    pylab.plot(G0_train[:,0:1].read().val[:,0], pheno_train3.val,".",G0_train[:,0:1].read().val[:,0],predicted_pheno.val,".")
    pylab.show()


    pheno_actual = pheno_train3.val[:,0]
    pylab.plot(pheno_actual, predicted_pheno.val,".")
    pylab.show()


    print "done"