"""Single phase PV-DER code."""

from __future__ import division
import numpy as np
import math
import cmath
import scipy
import six
import pdb
import warnings

from scipy.optimize import fsolve, minimize

from pvder.DER_components_three_phase import PV_Module
from pvder.DER_check_and_initialize import PVDER_SetupUtilities
from pvder.DER_features import PVDER_SmartFeatures
from pvder.DER_utilities import PVDER_ModelUtilities
from pvder.grid_components import Grid

from pvder import utility_functions

class SolarPV_DER_SinglePhase(PV_Module,PVDER_SetupUtilities,PVDER_SmartFeatures,PVDER_ModelUtilities,Grid):
    """
    Class for describing a Solar Photo-voltaic Distributed Energy Resource consisting of panel, converters, and
    control systems.
    
    Attributes:
         DER_count (int): Number of `SolarPV_DER_SinglePhase` instances.
         Ioverload (float): Overload current rating of inverter.
    """
    
    DER_count = 0
    #Number of ODE's
    n_ODE = 11
    
    #PLL controller parameters
    Kp_PLL = 180 #1800
    Ki_PLL = 320 #32000
    
    #Inverter current overload rating (Max 10s)
    Ioverload = 1.5
    inverter_ratings = {'10':{'Varated':250.0,'Vdcrated':550.0},
                        }
    
    circuit_parameters = {'10':{'Rf_actual':0.002,'Lf_actual' :25.0e-6,'C_actual':300.0e-6,'Z1_actual':0.0019 + 1j*0.0561},
                          }
    
    controller_parameters = {'10':{'scale_Kp_GCC':0.025,'scale_Ki_GCC':0.025,\
                                   'scale_Kp_DC':0.025,'scale_Ki_DC' : 0.025,\
                                   'scale_Kp_Q' : 0.025,'scale_Ki_Q' : 0.025,'wp' : 20e4},
                             }
    Sinverter_list = inverter_ratings.keys()
    #Frequency
    winv = we = 2.0*math.pi*60.0
    fswitching  = 10e3
    
    #Time delay before activating logic for MPP, Volt-VAR control,  LVRT/LFRT 
    t_stable = 1.0
    
    #Duty cycle
    m_steady_state = 0.96 #Expected duty cycle at steady state    
    
    def __init__(self,events,grid_model=None,\
                             Sinverter_rated = 10.0e3,Vrms_rated = None,
                             ia0 = 0+0j,xa0 =0+0j , ua0 = 0+0j,\
                             xDC0 = 0,xQ0 = 0,xPLL0 = 0.0,wte0 = 2*math.pi,\
                             gridVoltagePhaseA = None,\
                             gridVoltagePhaseB = None,\
                             gridVoltagePhaseC = None,\
                             gridFrequency = None,\
                             standAlone=True,STEADY_STATE_INITIALIZATION=False,\
                             pvderConfig=None,identifier=None): 
        
        """Creates an instance of `SolarPV_DER`.
        
        Args:
          events: An instance of `SimulationEvents`.
          grid_model: An instance of `GridModel`(only need to be suppled for stand alone simulation).
          Sinverter_rated: A scalar specifying the rated power (VA) of the DER.
          ia0,xa0,ua0: Complex scalars specifying the initial value of inverter states in the DER instance.
          xDC0,xQ0,xPLL0,wte0: Real scalars specifying the initial value of inverter states in the DER instance.
          gridVoltatePhaseA,gridVoltatePhaseA,gridVoltatePhaseA = Complex scalar specifying initial voltage phasor (V) at PCC - LV side from external program (only need to be suppled if model is not stand alone).
          standAlone: A boolean specifying if the DER instance is a stand alone simulation or part of a larger simulation.
          STEADY_STATE_INITIALIZATION: A boolean specifying whether states in the DER instance will be initialized to steady state values.
          pvderConfig: A dictionary containing configuration parameters that may be supplied from an external program.
          identifier: An identifier that can be used to name the instance (can be None).
          
        Raises:
          ValueError: If parameters corresponding to `Sinverter_rated` are not available.
          ValueError: If rated DC link voltage is not sufficient.
        """
        
        self.standAlone = standAlone
        self.update_grid_measurements(gridVoltagePhaseA, gridVoltagePhaseB, gridVoltagePhaseC,gridFrequency)
        self.Vrms_rated = Vrms_rated       
        
        #Increment count to keep track of number of PV-DER model instances
        SolarPV_DER_SinglePhase.DER_count = SolarPV_DER_SinglePhase.DER_count+1
        #Generate a name for the instance
        self.name_instance(identifier)
        
        if six.PY3:
            super().__init__(events,Sinverter_rated)  #Initialize PV module class (base class)
        elif six.PY2:
            super(SolarPV_DER_SinglePhase,self).__init__(events,Sinverter_rated)
        
        self.STEADY_STATE_INITIALIZATION = STEADY_STATE_INITIALIZATION
        
        self.attach_grid_model(grid_model)
        self.initialize_DER(Sinverter_rated,pvderConfig)
        
        self.LVRT_initialize() #LVRT settings
        self.initialize_jacobian()
        self.reset_reference_counters()
        
        #Reference
        self.Q_ref = self.get_Qref(t=0.0)
        
        #DC link voltage
        self.Vdc = self.Vdc_ref
        #PV module power output
        self.Ppv = self.Ppv_calc(self.Vdc_actual)
        
        self.initialize_states(ia0,xa0,ua0,xDC0,xQ0,xPLL0,wte0) #initialize_states

        self.update_voltages()
        self.update_power()        
        self.update_RMS()

        #Reference currents
        self.update_iref() 
        
        self.update_inverter_frequency(t=0.0)        
        
    @property                         #Decorator used for auto updating
    def y0(self):
        """List of initial states"""
        return  [self.ia.real, self.ia.imag, self.xa.real, self.xa.imag, self.ua.real,self.ua.imag,\
                 self.Vdc,self.xDC,self.xQ,self.xPLL,self.wte]

    #Apparent power output at inverter terminal
    def S_calc(self):
        """Inverter apparent power output"""
        
        return (1/2)*(self.vta*self.ia.conjugate())*1.0
        #return utility_functions.S_calc(self.vta,self.vtb,self.vtc,self.ia,self.ib,self.ic)
        
    #Apparent power output at PCC - LV side
    def S_PCC_calc(self):
        """Power output at PCC LV side"""
        return (1/2)*(self.va*self.ia.conjugate())
        #return utility_functions.S_calc(self.va,self.vb,self.vc,self.ia,self.ib,self.ic)
        
    def S_load1_calc(self):
        """Power absorbed by load at PCC LV side."""
        
        return (1/2)*(self.va*(-(self.va/self.Zload1)).conjugate())
    
    def S_G_calc(self):
        """Power absorbed/produced by grid voltage source."""
    
        return (1/2)*(-(self.ia-(self.va/self.Zload1))/self.a).conjugate()*self.grid_model.vag
    
    #@property
    def Vtrms_calc(self):
        """Inverter terminal voltage -  RMS"""
        
        return utility_functions.Urms_calc(self.vta,self.vta,self.vta)
    
    def Vrms_calc(self):
        """PCC LV side voltage - RMS"""
        
        return utility_functions.Urms_calc(self.va,self.va,self.va)
    
    def Irms_calc(self):
        """Inverter current - RMS"""
        
        return utility_functions.Urms_calc(self.ia,self.ia,self.ia)
    
    def Vabrms_calc(self):
        """PCC LV side voltage - line to line  RMS"""
        
        return abs(self.va-self.vb)/math.sqrt(2)
    
    def update_inverter_states(self,ia,xa,ua,Vdc,xDC,xQ,xPLL,wte):
        """Update inverter states
        
        Args:
             ia (complex): Inverter phase a current.
             xa (complex): Inverter controller state.
             ua (complex): Inverter controller state.
             Vdc (float): DC link voltage.             
        """
        
        self.ia = ia
        self.xa = xa
        self.ua = ua
        
        self.Vdc = Vdc
        self.xDC = xDC
        self.xQ = xQ
        
        self.xPLL = xPLL
        self.wte = wte

    def update_voltages(self):
        """Update voltages."""
        
        #Update inverter terminal voltage
        self.vta = self.vta_calc()
                
        #Update PCC LV side voltage
        self.va = self.va_calc()
        self.vb = self.vb_calc()
        self.vc = self.vc_calc()
        
    def update_RMS(self):
        """Update RMS voltages."""
        
        self.Vtrms = self.Vtrms_calc()
        self.Vrms = self.Vrms_calc()
        self.Irms = self.Irms_calc()
        
        #Update RMS values
        if self.DO_EXTRA_CALCULATIONS:
            self.Vabrms = self.Vabrms_calc()        
    
    def update_power(self):
        """Update RMS voltages."""
        
        #Update power output
        self.S = self.S_calc()
        self.S_PCC = self.S_PCC_calc()
        
        if self.standAlone:  #Update load current and grid voltage source power only in stand alone mode
            self.iaload1 = self.iphload1_calc(self.va)
            
            self.S_G = self.S_G_calc()
            self.S_load1 = self.S_load1_calc()     
    
    def update_iref(self):
        """Update reference reference current."""
    
        #Get current controller setpoint
        self.ia_ref = self.ia_ref_calc()
    
    def update_inverter_frequency(self,t):
        """Update inverter PLL frequency.
        
        Args:
             t (float): Simulation time in seconds.
        """
        #Update grid frequency
        self.wgrid_measured = self.wgrid_calc()
        
        #Convert PCC LV side voltage from phasor to alpha-beta domain
        self.valpha = utility_functions.phasor_to_time_1phase(self.va,w=self.wgrid_measured,t=t)
        self.vbeta =utility_functions. phasor_to_time_1phase(self.va*pow(math.e,-1j*(math.pi/2)),w=self.wgrid_measured,t=t)
        
        #Convert from alpha-beta domain to d-q domain using Parks transformation
        self.vd,self.vq = utility_functions.alpha_beta_to_d_q(self.valpha,self.vbeta,self.wte)
        
        #Calculate inverter frequency from PLL equation
        self.we = self.we_calc()
        self.winv = self.we
    
    def ODE_model(self,y,t):
        """System of ODE's defining the dynamic DER model.
        
        Args:
             y (list of float): Initial conditions for the states..
             t (float): Simulation time in seconds.
             
        Returns:
             result (list of float): Derivates for the system of ODE's.
        """
        iaR, iaI, xaR, xaI, uaR, uaI,\
        Vdc, xDC, xQ, xPLL, wte = y   # unpack current values of y
        
        self.update_inverter_states(iaR + 1j*iaI, xaR + 1j*xaI,uaR + 1j*uaI,\
                                    Vdc,xDC,xQ,\
                                    xPLL,wte)
       
        self.update_Ppv(t)
        self.update_Zload1(t) 
        
        self.update_voltages()
        self.update_power()    
        self.update_RMS()          
        
        self.update_Q_Vdc_ref(t)  
        self.update_iref()
        
        self.update_inverter_frequency(t)
        self.update_ridethrough_flags(t)
        
        #Phase a inverter output current
        diaR = (1/self.Lf)*(-self.Rf*self.ia.real - self.va.real + self.vta.real) + (self.winv/self.wbase)*self.ia.imag 
        diaI = (1/self.Lf)*(-self.Rf*self.ia.imag - self.va.imag + self.vta.imag) - (self.winv/self.wbase)*self.ia.real  
       
        #Current controller dynamics
        if abs(self.Kp_GCC*self.ua + self.xa)>self.m_limit*1e1:
            if np.sign(self.Ki_GCC*self.ua.real) == np.sign(self.xa.real):
                dxaR = 0.0
            else:
                dxaR = self.Ki_GCC*self.ua.real
            if np.sign(self.Ki_GCC*self.ua.imag) == np.sign(self.xa.imag):
                dxaI = 0.0
            else:
                dxaI = self.Ki_GCC*self.ua.imag
                #six.print_(dxaR+1j*dxaI,np.sign(self.Ki_GCC*self.ua))
        else:
            dxaR = self.Ki_GCC*self.ua.real
            dxaI = self.Ki_GCC*self.ua.imag
            
        if abs(self.Kp_GCC*self.ua + self.xa)>self.m_limit*1e1:
            if np.sign( (self.wp)*(-self.ua.real +  self.ia_ref.real - self.ia.real)) == np.sign(self.ua.real):
                duaR = 0.0
            else:
                duaR = (self.wp)*(-self.ua.real +  self.ia_ref.real - self.ia.real)
                
            if np.sign((self.wp)*(-self.ua.imag +  self.ia_ref.imag - self.ia.imag)) == np.sign(self.ua.imag):
                duaI = 0.0
            else:
                duaI = (self.wp)*(-self.ua.imag +  self.ia_ref.imag - self.ia.imag)
                
        else:
            duaR = (self.wp)*(-self.ua.real +  self.ia_ref.real - self.ia.real)
            duaI = (self.wp)*(-self.ua.imag +  self.ia_ref.imag - self.ia.imag)
        
        #DC link voltage dynamics
        dVdc = (self.Ppv - self.S.real)/(self.Vdc*self.C)
        
        if abs(self.xDC + self.Kp_DC*(self.Vdc_ref - self.Vdc) + 1j*(self.xQ  - self.Kp_Q*(self.Q_ref - self.S_PCC.imag)))>self.iref_limit:
            if np.sign(self.Ki_DC*(self.Vdc_ref - self.Vdc)) == np.sign(self.xDC):
                dxDC = 0.0
            else:
                dxDC = self.Ki_DC*(self.Vdc_ref - self.Vdc)
        else:
            dxDC = self.Ki_DC*(self.Vdc_ref - self.Vdc)
            
        # Reactive power controller dynamics
        if abs(self.xDC + self.Kp_DC*(self.Vdc_ref - self.Vdc) + 1j*(self.xQ  - self.Kp_Q*(self.Q_ref - self.S_PCC.imag)))>self.iref_limit:
            
            if np.sign(-self.Ki_Q*(self.Q_ref - self.S_PCC.imag)) == np.sign(self.xQ):
                dxQ = 0.0
            else:
                dxQ = -self.Ki_Q*(self.Q_ref - self.S_PCC.imag)
        else:
            dxQ = -self.Ki_Q*(self.Q_ref - self.S_PCC.imag)
        
        #SRF-PLL dynamics
        dxPLL = self.Ki_PLL*(self.vd)
        
        #Frequency integration to get angle
        dwte = self.we
        
        result =     [ diaR,# list of dy/dt=f functions
                       diaI,
                       dxaR,
                       dxaI,
                       duaR,
                       duaI,
                       dVdc,
                       dxDC,
                       dxQ,
                       dxPLL,
                       dwte]
        
        return result

    def jac_ODE_model(self,y,t):
        """Jacobian for the system of ODE's.
                
        Args:
             y (list of float): Initial conditions for the states..
             t (float): Simulation time in seconds.
             
        Returns:
             result (array of float): An array containing the elements of the Jacobian.
        """
        
        iaR, iaI, xaR, xaI, uaR, uaI,\
        Vdc, xDC, xQ, xPLL, wte = y   # unpack current values of y
        
        self.update_inverter_states(iaR + 1j*iaI, xaR + 1j*xaI,uaR + 1j*uaI,\
                                    Vdc,xDC,xQ,\
                                    xPLL,wte)
        
        J = self.J
        varInd = self.varInd 
        self.update_Ppv(t)
        #self.update_Zload1(t) 
        
        self.update_voltages()
        self.update_power()
        self.update_RMS()
        
        self.update_Q_Vdc_ref(t) 
        self.update_iref()
        #d-q transformation
        
        self.update_inverter_frequency(t)
       
        #LVRT trip logic
        if self.LVRT_TRIP == True and self.LVRT_RECONNECT == False:
            self.PV_DER_disconnect()
        #LFRT trip logic
        if self.LFRT_TRIP == True and self.LFRT_RECONNECT == False:
            self.PV_DER_disconnect() 
        
        #Phase a inverter output current
        
        ra,theta_a = cmath.polar(self.va)

        theta_a = self.wgrid_measured*t + theta_a - math.pi/2
            
        J[varInd['iaR'],varInd['iaR']] = -self.Rf/self.Lf            
        J[varInd['iaR'],varInd['iaI']] = (self.xPLL+self.Kp_PLL*self.vd+2*math.pi*60)/self.wbase
        J[varInd['iaR'],varInd['xaR']] = self.Vdc/(2*self.Lf)
        J[varInd['iaR'],varInd['uaR']] = (self.Vdc*self.Kp_GCC)/(2*self.Lf)
        J[varInd['iaR'],varInd['Vdc']] = (self.xa.real+self.ua.real*self.Kp_GCC)/(2*self.Lf)
        J[varInd['iaR'],varInd['xPLL']] = self.ia.imag/self.wbase
        J[varInd['iaR'],varInd['wte']] = ((self.Kp_PLL*self.ia.imag*ra)/self.wbase)*(-math.cos(theta_a)*math.sin(self.wte)
                                                                                    + math.cos(theta_a-math.pi/2)*math.cos(self.wte))
        
        J[varInd['iaI'],varInd['iaR']]= -(self.xPLL+self.Kp_PLL*self.vd+2*math.pi*60)/self.wbase
        J[varInd['iaI'],varInd['iaI']]= -self.Rf/self.Lf
        J[varInd['iaI'],varInd['xaI']]= self.Vdc/(2*self.Lf) 
        J[varInd['iaI'],varInd['uaI']]= (self.Vdc*self.Kp_GCC)/(2*self.Lf)
        J[varInd['iaI'],varInd['Vdc']]= (self.xa.imag+self.ua.imag*self.Kp_GCC)/(2*self.Lf)
        J[varInd['iaI'],varInd['xPLL']]= -self.ia.real/self.wbase
        J[varInd['iaI'],varInd['wte']] = ((self.Kp_PLL*self.ia.real*ra)/self.wbase)*(-math.cos(theta_a)*math.sin(self.wte)
                                                                                    + math.cos(theta_a-math.pi/2)*math.cos(self.wte))
            
        #Current controller dynamics
        if abs(self.Kp_GCC*self.ua + self.xa)>self.m_limit*1e1:
            if np.sign(self.Ki_GCC*self.ua.real) == np.sign(self.xa.real):
                J[varInd['xaR'],varInd['uaR']]=0.0
            else:
                J[varInd['xaR'],varInd['uaR']]=self.Ki_GCC
            if np.sign(self.Ki_GCC*self.ua.imag) == np.sign(self.xa.imag):
                J[varInd['xaI'],varInd['uaI']]=0.0
            else:
                J[varInd['xaI'],varInd['uaI']]=self.Ki_GCC
                
        else:
                J[varInd['xaR'],varInd['uaR']]=self.Ki_GCC
                J[varInd['xaI'],varInd['uaI']]=self.Ki_GCC
        
        if abs(self.Kp_GCC*self.ua + self.xa)>self.m_limit*1e1:
            if np.sign( (self.wp)*(-self.ua.real +  self.ia_ref.real - self.ia.real)) == np.sign(self.ua.real):
                J[varInd['uaR'],varInd['iaR']]= 0.0
                J[varInd['uaR'],varInd['uaR']]= 0.0
                J[varInd['uaR'],varInd['Vdc']]= 0.0
                J[varInd['uaR'],varInd['xDC']]= 0.0   
            else:
                #duaR = (self.wp)*(-self.ua.real +  self.ia_ref.real - self.ia.real)
                J[varInd['uaR'],varInd['iaR']]= -self.wp
                J[varInd['uaR'],varInd['uaR']]= -self.wp
                J[varInd['uaR'],varInd['Vdc']]= -self.wp*self.Kp_DC
                J[varInd['uaR'],varInd['xDC']]= self.wp                    
                    
            if np.sign((self.wp)*(-self.ua.imag +  self.ia_ref.imag - self.ia.imag)) == np.sign(self.ua.imag):
                #duaI = 0.0
                J[varInd['uaI'],varInd['iaR']]= 0.0
                J[varInd['uaI'],varInd['iaI']]= 0.0
                J[varInd['uaI'],varInd['uaI']]= 0.0
                J[varInd['uaI'],varInd['xQ']]= 0.0

            else:
                #duaI = (self.wp)*(-self.ua.imag +  self.ia_ref.imag - self.ia.imag)
                J[varInd['uaI'],varInd['iaR']]= (self.Kp_Q*self.wp*self.va.imag/2)
                J[varInd['uaI'],varInd['iaI']]= -self.wp - (self.Kp_Q*self.wp*self.va.real/2)
                J[varInd['uaI'],varInd['uaI']]= -self.wp
                J[varInd['uaI'],varInd['xQ']]= self.wp
                
        else:
            #duaR = (self.wp)*(-self.ua.real +  self.ia_ref.real - self.ia.real)
            #duaI = (self.wp)*(-self.ua.imag +  self.ia_ref.imag - self.ia.imag)
            J[varInd['uaR'],varInd['iaR']]= -self.wp
            J[varInd['uaR'],varInd['uaR']]= -self.wp
            J[varInd['uaR'],varInd['Vdc']]= -self.wp*self.Kp_DC
            J[varInd['uaR'],varInd['xDC']]= self.wp   
                
            J[varInd['uaI'],varInd['iaR']]= (self.Kp_Q*self.wp*self.va.imag/2)
            J[varInd['uaI'],varInd['iaI']]= -self.wp - (self.Kp_Q*self.wp*self.va.real/2)
            J[varInd['uaI'],varInd['uaI']]= -self.wp
            J[varInd['uaI'],varInd['xQ']]= self.wp
         
        #DC link voltage dynamics
        dVdc = (self.Ppv - self.S.real)/(self.Vdc*self.C)
        J[varInd['Vdc'],varInd['iaR']]= -(self.xa.real+self.Kp_GCC*self.ua.real)/(4*self.C)
        J[varInd['Vdc'],varInd['iaI']]= -(self.xa.imag+self.Kp_GCC*self.ua.imag)/(4*self.C)
        J[varInd['Vdc'],varInd['xaR']]= -self.ia.real/(4*self.C)
        J[varInd['Vdc'],varInd['xaI']]= -self.ia.imag/(4*self.C)
        J[varInd['Vdc'],varInd['uaR']]= -(self.Kp_GCC*self.ia.real)/(4*self.C)
        J[varInd['Vdc'],varInd['uaI']]= -(self.Kp_GCC*self.ia.imag)/(4*self.C)
        J[varInd['Vdc'],varInd['Vdc']]= (-(self.q*self.Np*self.Irs*(self.Vdcbase**2))/(self.C*self.k*self.A*self.Ns*self.Tactual*self.Sbase))*math.exp((self.q*self.Vdc*self.Vdcbase)/(self.k*self.A*self.Ns*self.Tactual))
        #DC link voltage controller dynamics
        if abs(self.xDC + self.Kp_DC*(self.Vdc_ref - self.Vdc) + 1j*(self.xQ  - self.Kp_Q*(self.Q_ref - self.S_PCC.imag)))>self.iref_limit:
            if np.sign(self.Ki_DC*(self.Vdc_ref - self.Vdc)) == np.sign(self.xDC):
                #dxDC = 0.0
                J[varInd['xDC'],varInd['Vdc']]= 0.0
            else:
                #dxDC = self.Ki_DC*(self.Vdc_ref - self.Vdc)
                J[varInd['xDC'],varInd['Vdc']]=-self.Ki_DC
        else:
            #dxDC = self.Ki_DC*(self.Vdc_ref - self.Vdc)
            J[varInd['xDC'],varInd['Vdc']]=-self.Ki_DC
            
        # Reactive power controller dynamics
        if abs(self.xDC + self.Kp_DC*(self.Vdc_ref - self.Vdc) + 1j*(self.xQ  - self.Kp_Q*(self.Q_ref - self.S_PCC.imag)))>self.iref_limit:
            
            if np.sign(-self.Ki_Q*(self.Q_ref - self.S_PCC.imag)) == np.sign(self.xQ):
                #dxQ = 0.0
                J[varInd['xQ'],varInd['iaR']]= 0.0
                J[varInd['xQ'],varInd['iaI']]= 0.0

            else:
                #dxQ = -self.Ki_Q*(self.Q_ref - self.S_PCC.imag)
                J[varInd['xQ'],varInd['iaR']]= (self.Ki_Q*self.va.imag/2)
                J[varInd['xQ'],varInd['iaI']]= -(self.Ki_Q*self.va.real/2)
        
        else:
            #dxQ = -self.Ki_Q*(self.Q_ref - self.S_PCC.imag)
            J[varInd['xQ'],varInd['iaR']]= (self.Ki_Q*self.va.imag/2)
            J[varInd['xQ'],varInd['iaI']]= -(self.Ki_Q*self.va.real/2)
        
        #SRF-PLL dynamics
        #dxPLL = self.Ki_PLL*(self.vd)
        J[varInd['xPLL'],varInd['wte']] = (self.Ki_PLL*ra)*(-math.cos(theta_a)*math.sin(self.wte)
                                                            + math.cos(theta_a-math.pi/2)*math.cos(self.wte))
        
        #Frequency integration to get angle
        #dwte = self.we
        J[varInd['wte'],varInd['xPLL']]= 1
        J[varInd['wte'],varInd['wte']] = (self.Kp_PLL*ra)*(-math.cos(theta_a)*math.sin(self.wte)
                                                            + math.cos(theta_a-math.pi/2)*math.cos(self.wte))
        
        return J    