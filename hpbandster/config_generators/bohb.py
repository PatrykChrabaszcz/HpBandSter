import logging
from copy import deepcopy
import traceback


import ConfigSpace
import numpy as np
import scipy.stats as sps
import scipy.optimize as spo
import statsmodels.api as sm

from hpbandster.config_generators.base import base_config_generator


from pdb import set_trace
#from IPython import embed



class BOHB(base_config_generator):
	
	def __init__(self, configspace, min_points_in_model = None,
				 top_n_percent=15, num_samples = 64, random_fraction=1/3,
				 bandwidth_factor=3,
				**kwargs):
		"""
			Fits for each given budget a kernel density estimator on the best N percent of the
			evaluated configurations on this budget.


			Parameters:
			-----------
			configspace: ConfigSpace
				Configuration space object
			top_n_percent: int
				Determines the percentile of configurations that will be used as training data
				for the kernel density estimator, e.g if set to 10 the 10% best configurations will be considered
				for training.
			min_points_in_model: int
				minimum number of datapoints needed to fit a model
			num_samples: int
				number of samples drawn to optimize EI via sampling
			random_fraction: float
				fraction of random configurations returned
			bandwidth_factor: float
				widens the bandwidth for contiuous parameters for proposed points to optimize EI

		"""
		super().__init__(**kwargs)
		self.top_n_percent=top_n_percent
		self.configspace = configspace
		self.bw_factor = bandwidth_factor


		self.min_points_in_model = min_points_in_model
		if min_points_in_model is None:
			self.min_points_in_model = len(self.configspace.get_hyperparameters())+1

		self.num_samples = num_samples
		self.random_fraction = random_fraction


		hps = self.configspace.get_hyperparameters()

		self.kde_vartypes = ""
		self.vartypes = []


		for h in hps:
			if hasattr(h, 'choices'):
				self.kde_vartypes += 'u'
				self.vartypes +=[ len(h.choices)]
			else:
				self.kde_vartypes += 'c'
				self.vartypes +=[0]
		
		self.vartypes = np.array(self.vartypes, dtype=int)

		# store precomputed probs for the categorical parameters
		self.cat_probs = []
		

		self.configs = dict()
		self.losses = dict()
		self.good_config_rankings = dict()
		self.kde_models = dict()
		
	def get_config(self, budget):
		"""
			Function to sample a new configuration

			This function is called inside Hyperband to query a new configuration


			Parameters:
			-----------
			budget: float
				the budget for which this configuration is scheduled

			returns: config
				should return a valid configuration

		"""
		sample = None
		info_dict = {}
		
		# If no model is available, sample from prior
		# also mix in a fraction of random configs
		if len(self.kde_models.keys()) == 0 or np.random.rand() < self.random_fraction:
			sample =  self.configspace.sample_configuration().get_dictionary()
			info_dict['model_based_pick'] = False

		best = np.inf
		best_vector = None

		if sample is None:
			try:

				# If we haven't seen anything with this budget, we sample from the kde trained on the highest budget
				#if budget not in self.kde_models.keys():
				#    budget = max(self.kde_models.keys())

				#sample from largest budget
				budget = max(self.kde_models.keys())

				l = self.kde_models[budget]['good'].pdf
				g = self.kde_models[budget]['bad' ].pdf
			
				minimize_me = lambda x: max(1e-8, g(x))/max(l(x), 1e-8)
				
				kde_good = self.kde_models[budget]['good']

				for i in range(self.num_samples):
					#idx = np.random.choice(range(kde_good.data.shape[0]), 1)[0]
					idx = np.random.randint(0, len(kde_good.data))

					vector = []
					
					for m,bw,t in zip(kde_good.data[idx], kde_good.bw, self.vartypes):
						if t == 0:
							vector.append(sps.truncnorm.rvs(-m/bw,(1-m)/bw, loc=m, scale=self.bw_factor*bw))
						else:
							
							if np.random.rand() < (1-bw):
								vector.append(m)
							else:
								vector.append(np.random.randint(t))
					
					val = minimize_me(vector) 
					if val < best:
						best = val
						best_vector = vector

				if best_vector is None:
					self.logger.debug("Sampling based optimization with %i samples failed -> using random configuration"%self.num_samples)
					sample = self.configspace.sample_configuration().get_dictionary()
					info_dict['model_based_pick']  = False
				else:
					self.logger.debug('best_vector: {}, {}'.format(best_vector, best))
					sample = ConfigSpace.Configuration(self.configspace, vector=best_vector).get_dictionary()
					info_dict['model_based_pick'] = True

			except:
				self.logger.warning("Sampling based optimization with %i samples failed\n %s \nUsing random configuration"%(self.num_samples, traceback.format_exc()))
				sample = self.configspace.sample_configuration().get_dictionary()
				info_dict['model_based_pick']  = False


		return sample, info_dict

	def new_result(self, job):
		"""
			function to register finished runs

			Every time a run has finished, this function should be called
			to register it with the result logger. If overwritten, make
			sure to call this method from the base class to ensure proper
			logging.


			Parameters:
			-----------
			job: hpbandster.distributed.dispatcher.Job object
				contains all the info about the run
		"""

		super().new_result(job)

		if job.result is None:
			# One could skip crashed results, but we decided 
			# assign a +inf loss and count them as bad configurations
			loss = np.inf
		else:
			loss = job.result["loss"]

		budget = job.kwargs["budget"]

		if budget not in self.configs.keys():
			self.configs[budget] = []
			self.losses[budget] = []


		# skip model building if we already have a bigger model
		if max(list(self.kde_models.keys()) + [-np.inf]) > budget:
			return



		# We want to get a numerical representation of the configuration in the original space

		conf = ConfigSpace.Configuration(self.configspace, job.kwargs["config"])
		self.configs[budget].append(conf.get_array())
		self.losses[budget].append(loss)
		

		if len(self.configs[budget]) <= self.min_points_in_model+1:
			return


		train_configs = np.array(self.configs[budget])
		train_losses =  np.array(self.losses[budget])

		#n_good= max(len(self.configspace.get_hyperparameters())+1, int(max(1, np.sqrt(len(train_configs))/4)))
		n_good= max(self.min_points_in_model, (self.top_n_percent * train_configs.shape[0])//100 )
		n_bad = max(self.min_points_in_model, ((100-self.top_n_percent)*train_configs.shape[0])//100)


		# Refit KDE for the current budget
		idx = np.argsort(train_losses)

		train_data_good = train_configs[idx[:n_good]]
		train_data_bad  = train_configs[idx[-n_bad:]]

		if train_data_good.shape[0] <= train_data_good.shape[1]:
			return
		if train_data_bad.shape[0] <= train_data_bad.shape[1]:
			return
		
		#more expensive crossvalidation method
		#bw_estimation = 'cv_ls'

		# quick rule of thumb
		bw_estimation = 'normal_reference'

		bad_kde = sm.nonparametric.KDEMultivariate(data=train_data_bad,  var_type=self.kde_vartypes, bw=bw_estimation)
		good_kde = sm.nonparametric.KDEMultivariate(data=train_data_good, var_type=self.kde_vartypes, bw=bw_estimation)

		self.kde_models[budget] = {
				'good': good_kde,
				'bad' : bad_kde
		}

		# update probs for the categorical parameters for later sampling
		self.logger.debug('done building a new model for budget %f based on %i/%i split\nBest loss for this budget:%f\n\n\n\n\n'%(budget, n_good, n_bad, np.min(train_losses)))
