import numpy as np
import scipy as sp
import pandas as pd
import scipy
import scipy.stats
from scipy.stats import norm
from scipy import integrate
import math
import random
import matplotlib.pyplot as plt
import pickle

class EM:
    def __init__(self, y, X, c, J, K, train_pct, single_effect=False, init_A_mean=.1, init_b_mean=-.1):
        # Store inputs
        self.num_patients = np.shape(y)[0] 
        self.T = np.shape(y)[1] # length of the observed sequence
        self.y = y
        self.X = X # treatment matrix
        self.c = c # chronic conditions
        self.K = K  # number of interaction terms modeled
        self.J = J # number of past treatment effects to be considered
        self.single_effect = single_effect # if true only consider the Jth treatment prior to the current time point

        self.train_pct = train_pct # percentage of each time series used for training
        # time of last observations for each patients plus one
        # plus one because last_obs and last_train_obs effectively take the role of T in the most basic version
        # T represents the position right after the end of 'valid' values in an array 
        self.last_obs = self.find_last_obs()
        # time of last observation for training plus one 
        self.last_train_obs = self.find_last_train_obs()
        
        # Other model parameters
        self.N = np.shape(self.X)[2] # number of treatments
        self.M = np.shape(self.c)[1] # number of chronic conditions
        self.Q = np.zeros((self.num_patients, self.T, self.K)) # interaction term
        
        # Model Parameters to be estimated
        if self.single_effect:
            self.A = np.full(self.N, init_A_mean) + np.random.randn(self.N)*0.01
        else:
            self.A = np.full((self.J, self.N), init_A_mean) + np.random.randn(self.J, self.N)*0.01 # coefficients a_j's
        self.b = np.full(self.M, init_b_mean) + np.random.randn(self.M)*0.01
        self.d = np.zeros(self.K)
        self.sigma_1 = .05
        self.sigma_2 = .005
        self.sigma_0 = .05 # initial state variance
        self.init_z = np.random.normal(0, np.sqrt(self.sigma_0), size = 1)# np.random.uniform(0, 10, size = 1) # initial state mean
        
        # create coefficient matrix
        mtx = []
        for n in range(self.num_patients):
            columns = []
            if self.single_effect:
                self.params = np.zeros(self.N + self.M + self.K)
                col = np.roll(self.X[n, :, :], shift=self.J, axis=0)
                col[[i for i in range(0, self.J)], :] = 0
                columns.append(col)
            else:
                self.params = np.zeros(self.N*self.J + self.M + self.K)
                for j in range(1, self.J+1):
                    col = np.roll(self.X[n, :, :], shift=j, axis=0)
                    col[[i for i in range(0, j)], :] = 0
                    columns.append(col)
            C = np.stack([self.c[n, :] for i in range(self.T)], axis = 0)
            columns.append(C)
            coeff_mtx = np.concatenate(columns, axis = 1)
            # get rid of nans
            coeff_mtx = coeff_mtx[:self.last_train_obs[n], :]
            nans = np.where(np.isnan(self.y[n, :self.last_train_obs[n]]))[0]
            coeff_mtx = np.delete(coeff_mtx, nans, axis = 0)
            mtx.append(coeff_mtx)
        self.coeff_mtx = np.concatenate(mtx, axis = 0)        
        
        # Intermediate values to stored
        self.mu_filter = np.zeros((self.num_patients, self.T)) # mu_t|t
        self.sigma_filter = np.zeros((self.num_patients, self.T)) # sigma^2_t|t
        self.kgain = np.zeros((self.num_patients, self.T)) # K_t, kalman gain
        self.jgain = np.zeros((self.num_patients, self.T)) # J_t, backward kalman gain 
        self.mu_smooth = np.zeros((self.num_patients, self.T)) # mu_t|T
        self.sigma_smooth = np.zeros((self.num_patients, self.T)) # sigma^2_t|T
        self.mu_square_smooth = np.zeros((self.num_patients, self.T)) # E[z_t^2|{y}]
        self.mu_ahead_smooth = np.zeros((self.num_patients, self.T)) # E[z_t * z_{t-1}|{y}]
        self.sigma_ahead_smooth = np.zeros((self.num_patients, self.T))

        self.log_lik = [] # used to debug
    
    # find the last non-nan y value for training for each patient
    # return an array of the length num_patients
    def find_last_obs(self):
        last_non_nan = np.zeros(self.y.shape[0], dtype=int)
        for i in range(self.y.shape[0]):
            last_non_nan[i] = np.where(np.invert(np.isnan(self.y[i, :])))[0][-1] + 1
        return last_non_nan
    
    def find_last_train_obs(self):
        last_train_obs = np.zeros(self.y.shape[0], dtype=int)
        for i in range(self.y.shape[0]):
            non_nan_idx = np.where(np.invert(np.isnan(self.y[i, :])))[0]
            last_train_obs[i] = non_nan_idx[int(non_nan_idx.shape[0] * self.train_pct) - 1] + 1
        return last_train_obs
    
    # compute the added effect, denoted pi_t, at time t given the current parameter values 
    def added_effect(self, n, t):
        treatment_effect = 0
        if self.single_effect:
            treatment_effect = np.dot(self.A, self.X[n, t-self.J, :])
        else:
            for j in range(self.J):
                if t-1 >= j:
                    treatment_effect += np.dot(self.A[j, :], self.X[n, t-1-j, :])
        pi = treatment_effect + np.dot(self.b, self.c[n, :]) # total added effect
        return pi
    
    '''E Step Calculations'''
    # kalman filter update step
    def kfilter(self, n, t):
        if np.isnan(self.y[n, t+1]):
            self.mu_filter[n, t+1] = self.mu_filter[n, t]
            self.sigma_filter[n, t+1] = self.sigma_filter[n, t] + self.sigma_1
        else:
            mu_pred = self.mu_filter[n, t] 
            sigma_pred = self.sigma_filter[n, t] + self.sigma_1
            self.kgain[n, t+1] = sigma_pred / (sigma_pred + self.sigma_2)
            self.mu_filter[n, t+1] = mu_pred + self.kgain[n, t+1] * (self.y[n, t+1] - mu_pred - self.added_effect(n, t+1))
            self.sigma_filter[n, t+1] = (1 - self.kgain[n, t+1]) * sigma_pred
    
    # kalman filter for each time point, message passing forward
    def forward(self):
        for n in range(self.num_patients):
            self.mu_filter[n, 0] = self.init_z
            self.sigma_filter[n, 0] = self.sigma_0
            for t in range(self.last_train_obs[n]-1):
                self.kfilter(n, t)
    
    # kalman smoother update step
    def ksmoother(self, n, t):
        sigma_pred = self.sigma_filter[n, t] + self.sigma_1 # sigma^2_t+1|t
        self.jgain[n, t] = self.sigma_filter[n, t] / sigma_pred
        self.mu_smooth[n, t] = self.mu_filter[n, t] + self.jgain[n, t] * (self.mu_smooth[n, t+1] - self.mu_filter[n, t])
        self.sigma_smooth[n, t] = self.sigma_filter[n, t] + np.square(self.jgain[n, t]) * (self.sigma_smooth[n, t+1] - sigma_pred)
        self.mu_square_smooth[n, t] = self.sigma_smooth[n, t] + np.square(self.mu_smooth[n, t])
    
    def backward(self):
        for n in range(self.num_patients):
            self.mu_smooth[n, self.last_train_obs[n]-1] = self.mu_filter[n, self.last_train_obs[n]-1]
            self.sigma_smooth[n, self.last_train_obs[n]-1] = self.sigma_filter[n, self.last_train_obs[n]-1]
            self.mu_square_smooth[n, self.last_train_obs[n]-1] = self.sigma_smooth[n, self.last_train_obs[n]-1] + \
                np.square(self.mu_smooth[n, self.last_train_obs[n]-1])
            for t in range(self.last_train_obs[n]-2, -1, -1):
                self.ksmoother(n, t)
    
    # backward recursion to compute sigma^2_{t, t-1}|T, which is necessary to compute mu_ahead_smooth
    def backward_sigma_ahead(self):
        for n in range(self.num_patients):
            initial_time = self.last_train_obs[n]-2
            self.sigma_ahead_smooth[n, initial_time] = (1 - self.kgain[n, initial_time+1]) * self.sigma_filter[n, initial_time]
            for t in range(self.last_train_obs[n]-3, -1, -1):
                self.sigma_ahead_smooth[n, t] = self.sigma_filter[n, t+1] * self.jgain[n, t] + \
                    self.jgain[n, t+1] * (self.sigma_ahead_smooth[n, t+1] - self.sigma_filter[n, t+1]) * self.jgain[n, t]
    
    def calc_mu_ahead_smooth(self):
        for n in range(self.num_patients):
            for t in range(self.last_train_obs[n]-1):
                self.mu_ahead_smooth[n, t] = self.sigma_ahead_smooth[n, t] + self.mu_smooth[n, t] * self.mu_smooth[n, t+1]
    
    def E_step(self):
        self.forward()
        self.backward()
        self.backward_sigma_ahead()
        self.calc_mu_ahead_smooth()
    
    '''M Step Calculations'''
    def sigma_0_mle(self):
        result = 0
        for n in range(self.num_patients):
            result += self.mu_square_smooth[n, 0] - np.square(self.mu_smooth[n, 0])
        result /= self.num_patients
        self.sigma_0 = result
        
    def sigma_1_mle(self):
        result = 0
        for n in range(self.num_patients):
            sum_result = 0
            if self.last_train_obs[n] > 1:
                for t in range(self.last_train_obs[n]-1):
                    sum_result += self.mu_square_smooth[n, t+1]-2*self.mu_ahead_smooth[n, t]+self.mu_square_smooth[n, t]
                result += sum_result / (self.last_train_obs[n]-1) 
        result /= self.num_patients
        self.sigma_1 = result

    def init_z_mle(self):
        result = 0
        for n in range(self.num_patients):
            result += self.mu_smooth[n, 0]
        result /= self.num_patients
        self.init_z = result
    
    def pi_mle(self):
        rhs_list = []
        for n in range(self.num_patients):
            rhs = np.subtract(self.y[n, :self.last_train_obs[n]], self.mu_smooth[n, :self.last_train_obs[n]])
            rhs = np.delete(rhs, np.where(np.isnan(rhs))[0])
            rhs_list.append(rhs)
        rhs_concat = np.concatenate(rhs_list, axis = 0)
        params = np.linalg.lstsq(self.coeff_mtx, rhs_concat, rcond=None)[0] # params as a long vector
        if self.single_effect:
            self.A = np.array(params[0:self.N])
            self.b = np.array(params[self.N:self.N+self.M])
        else:
            self.A = np.reshape(params[0:self.N*self.J], (self.J, self.N))
            self.b = np.array(params[self.N*self.J:self.N*self.J+self.M])
            self.d = np.array(params[self.N*self.J+self.M:])
        self.params = params 
    
    def sigma_2_mle(self):
        result = 0
        for n in range(self.num_patients):
            sum_result = 0
            num_sum = 0
            for t in range(self.last_train_obs[n]):
                if not np.isnan(self.y[n, t]):
                    sum_result += np.square(self.y[n, t]-self.added_effect(n, t))-2*(self.y[n, t]-self.added_effect(n, t)) \
                        *self.mu_smooth[n, t] + self.mu_square_smooth[n, t]
                    num_sum += 1
            result += sum_result / num_sum
        result /= self.num_patients
        self.sigma_2 = result
        
    def M_step(self):
        self.init_z_mle()
        self.sigma_0_mle()
        self.sigma_1_mle()
        self.pi_mle()
        self.sigma_2_mle()
        
    '''Run EM for fixed iterations or until convergence'''
    def run_EM(self, max_num_iter, tol=.01):
        old_ll = -np.inf
        for i in range(max_num_iter):
            self.E_step()
            self.M_step()
            #print('iteration {}'.format(i))
            new_ll = self.obs_log_lik()
            #print('observed log likelihood {}'.format(new_ll))
            self.log_lik.append(new_ll)
            '''
            if np.abs(new_ll - old_ll) < tol:
                print('{} iterations before convergence'.format(i+1))
                return
            '''
            old_ll = new_ll
            '''
            diff = np.absolute(np.subtract(prev, self.params))
            if np.all(diff < tol) and abs(self.sigma_1 - prev_sigma_1) < tol and abs(self.sigma_2 - prev_sigma_2) < tol \
                and abs(self.init_z - prev_init_z) < tol and abs(self.sigma_0 - prev_sigma_0) < tol:
                print('{} iterations before convergence'.format(i+1))
                return
            '''
            #print('mse {}'.format(self.get_MSE()))
        print('max iterations: {} reached'.format(max_num_iter))
    
    def transition(self, prev, n, t):
        noise = self.sigma_filter[n, t-1] + self.sigma_1
        self.sigma_filter[n, t] = noise
        z = prev #np.random.normal(prev, np.sqrt(noise), 1)
        return z

    def emission(self, z, n, t):
        mean = z + self.added_effect(n, t)
        y = mean #np.random.normal(mean, np.sqrt(self.sigma_2), 1)
        return y
    
    # given parameters and latent state
    def predict(self, n):
        y = np.zeros(self.last_obs[n])
        z = np.zeros(self.last_obs[n])
        y[:self.last_train_obs[n]] = self.y[n, 0:self.last_train_obs[n]]
        z[:self.last_train_obs[n]] = self.mu_smooth[n, 0:self.last_train_obs[n]]
        z[self.last_train_obs[n]] = self.transition(z[self.last_train_obs[n]-1], n, self.last_train_obs[n])
        y[self.last_train_obs[n]] = self.emission(z[self.last_train_obs[n]], n, self.last_train_obs[n])
        for t in range(self.last_train_obs[n]+1, self.last_obs[n]):
            z[t] = self.transition(z[t-1], n, t)
            y[t] = self.emission(z[t], n, t)
        return z, y

    # get prediction mean square error 
    def get_MSE(self):
        sum_of_square = 0
        count = 0
        for n in range(self.num_patients):
            if self.last_train_obs[n] < self.last_obs[n]:
                y_true = self.y[n, self.last_train_obs[n]:self.last_obs[n]]
                y_pred = self.predict(n)[1][self.last_train_obs[n]:self.last_obs[n]]
                y_true = y_true[np.where(np.invert(np.isnan(y_true)))[0]]
                y_pred = y_pred[np.where(np.invert(np.isnan(y_true)))[0]]
                sum_of_square += np.sum(np.square(np.subtract(y_pred, y_true))) / y_pred.shape[0]
                count += 1
        if count > 0:
            return sum_of_square / count
        else:
            return 0


    # calculate the observed data (training) log likelihood 
    def obs_log_lik(self):
        log_lik = 0
        '''
        for n in range(self.num_patients):
            for t in range(self.last_train_obs[n]):
                if np.isnan(self.y[n, t]):
                    continue
                mean = self.mu_smooth[n, t] + self.added_effect(n, t)
                std = np.sqrt(self.sigma_2)
                log_lik += norm.logpdf(self.y[n, t], mean, std)

        for n in range(self.num_patients):
            idv_loglik = 0
            p_y1 = norm.logpdf(self.y[n, 0], self.init_z, np.sqrt(self.sigma_0))
            idv_loglik += p_y1
            for t in range(1, self.last_train_obs[n]):
                p_yt_zt = lambda zt: norm.pdf(self.y[n, t], zt+self.added_effect(n, t), np.sqrt(self.sigma_2))
                p_zt_zt_1 = lambda zt, zt_1: norm.pdf(zt, zt_1, np.sqrt(self.sigma_1))
                p_zt_1_yt_1 = lambda zt_1: norm.pdf(zt_1, self.mu_filter[n, t-1], np.sqrt(self.sigma_filter[n, t-1]))
                func = lambda zt, zt_1: p_yt_zt(zt) * p_zt_zt_1(zt, zt_1) * p_zt_1_yt_1(zt_1)
                idv_loglik += np.log(integrate.nquad(func, [[-np.inf, np.inf], [-np.inf, np.inf]]))
            log_lik += idv_loglik
        '''
        for n in range(self.num_patients):
            idv_loglik = 0
            p_y1 = norm.logpdf(self.y[n, 0], self.init_z, np.sqrt(self.sigma_0))
            idv_loglik += p_y1
            for t in range(1, self.last_train_obs[n]):
                p_yt_zt = norm.logpdf(self.y[n, t], self.mu_filter[n, t]+self.added_effect(n, t), np.sqrt(self.sigma_2))
                p_zt_zt_1 = norm.logpdf(self.mu_filter[n, t], self.mu_filter[n, t-1], np.sqrt(self.sigma_1))
                p_zt_1_yt_1 = norm.logpdf(self.mu_filter[n, t-1], self.mu_filter[n, t-1], np.sqrt(self.sigma_filter[n, t-1]))
                idv_loglik += p_yt_zt + p_zt_zt_1 + p_zt_1_yt_1
            log_lik += idv_loglik
        return log_lik
