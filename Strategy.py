def Initialize(self) -> None:

    #1. Required: Five years of backtest history
    self.SetStartDate(2014, 1, 1)

    #2. Required: Alpha Streams Models:
    self.SetBrokerageModel(BrokerageName.AlphaStreams)

    #3. Required: Significant AUM Capacity
    self.SetCash(1000000)

    #4. Required: Benchmark to SPY
    self.SetBenchmark("SPY")

    self.assets = ["SCHO", "SHY"]
    
    # Add Equity ------------------------------------------------ 
    for i in range(len(self.assets)):
        self.AddEquity(self.assets[i], Resolution.Minute)
        
    # Instantiate our model
    self.Recalibrate()
    
    # Set a variable to indicate the trading bias of the portfolio
    self.state = 0
    
    # Set Scheduled Event Method For Kalman Filter updating.
    self.Schedule.On(self.DateRules.WeekStart(), 
        self.TimeRules.At(0, 0), 
        self.Recalibrate)
    
    # Set Scheduled Event Method For Kalman Filter updating.
    self.Schedule.On(self.DateRules.EveryDay(), 
        self.TimeRules.BeforeMarketClose("SHY"), 
        self.EveryDayBeforeMarketClose)
        
        
def Recalibrate(self) -> None:
    qb = self
    history = qb.History(self.assets, 252*2, Resolution.Daily)
    if history.empty: return
    
    # Select the close column and then call the unstack method
    data = history['close'].unstack(level=0)
    
    # Convert into log-price series to eliminate compounding effect
    log_price = np.log(data)
    
    ### Get Cointegration Vectors
    # Get the cointegration vector
    coint_result = engle_granger(log_price.iloc[:, 0], log_price.iloc[:, 1], trend="c", lags=0)
    coint_vector = coint_result.cointegrating_vector[:2]
    
    # Get the spread
    spread = log_price @ coint_vector
    
    ### Kalman Filter
    # Initialize a Kalman Filter. Using the first 20 data points to optimize its initial state. We assume the market has no regime change so that the transitional matrix and observation matrix is [1].
    self.kalmanFilter = KalmanFilter(transition_matrices = [1],
                        observation_matrices = [1],
                        initial_state_mean = spread.iloc[:20].mean(),
                        observation_covariance = spread.iloc[:20].var(),
                        em_vars=['transition_covariance', 'initial_state_covariance'])
    self.kalmanFilter = self.kalmanFilter.em(spread.iloc[:20], n_iter=5)
    (filtered_state_means, filtered_state_covariances) = self.kalmanFilter.filter(spread.iloc[:20])
    
    # Obtain the current Mean and Covariance Matrix expectations.
    self.currentMean = filtered_state_means[-1, :]
    self.currentCov = filtered_state_covariances[-1, :]
    
    # Initialize a mean series for spread normalization using the Kalman Filter's results.
    mean_series = np.array([None]*(spread.shape[0]-20))
    
    # Roll over the Kalman Filter to obtain the mean series.
    for i in range(20, spread.shape[0]):
        (self.currentMean, self.currentCov) = self.kalmanFilter.filter_update(filtered_state_mean = self.currentMean,
                                                                filtered_state_covariance = self.currentCov,
                                                                observation = spread.iloc[i])
        mean_series[i-20] = float(self.currentMean)
    
    # Obtain the normalized spread series.
    normalized_spread = (spread.iloc[20:] - mean_series)
    
    ### Determine Trading Threshold
    # Initialize 50 set levels for testing.
    s0 = np.linspace(0, max(normalized_spread), 50)
    
    # Calculate the profit levels using the 50 set levels.
    f_bar = np.array([None]*50)
    for i in range(50):
        f_bar[i] = len(normalized_spread.values[normalized_spread.values > s0[i]]) \
            / normalized_spread.shape[0]
        
    # Set trading frequency matrix.
    D = np.zeros((49, 50))
    for i in range(D.shape[0]):
        D[i, i] = 1
        D[i, i+1] = -1
        
    # Set level of lambda.
    l = 1.0
    
    # Obtain the normalized profit level.
    f_star = np.linalg.inv(np.eye(50) + l * D.T@D) @ f_bar.reshape(-1, 1)
    s_star = [f_star[i]*s0[i] for i in range(50)]
    self.threshold = s0[s_star.index(max(s_star))]
    
    # Set the trading weight. We would like the portfolio absolute total weight is 1 when trading.
    self.trading_weight = coint_vector / np.sum(abs(coint_vector))
    
        
def EveryDayBeforeMarketClose(self) -> None:
    qb = self
    
    # Get the real-time log close price for all assets and store in a Series
    series = pd.Series()
    for symbol in qb.Securities.Keys:
        series[symbol] = np.log(qb.Securities[symbol].Close)
        
    # Get the spread
    spread = np.sum(series * self.trading_weight)
    
    # Update the Kalman Filter with the Series
    (self.currentMean, self.currentCov) = self.kalmanFilter.filter_update(filtered_state_mean = self.currentMean,
                                                                        filtered_state_covariance = self.currentCov,
                                                                        observation = spread)
        
    # Obtain the normalized spread.
    normalized_spread = spread - self.currentMean

    # ==============================
    
    # Mean-reversion
    if normalized_spread < -self.threshold:
        orders = []
        for i in range(len(self.assets)):
            orders.append(PortfolioTarget(self.assets[i], self.trading_weight[i]))
            self.SetHoldings(orders)
            
        self.state = 1
            
    elif normalized_spread > self.threshold:
        orders = []
        for i in range(len(self.assets)):
            orders.append(PortfolioTarget(self.assets[i], -1 * self.trading_weight[i]))
            self.SetHoldings(orders)
            
        self.state = -1
            
    # Out of position if spread recovered
    elif self.state == 1 and normalized_spread > -self.threshold or self.state == -1 and normalized_spread < self.threshold:
        self.Liquidate()
        
        self.state = 0
