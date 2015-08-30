'''
(*)~----------------------------------------------------------------------------------
 Pupil - eye tracking platform
 Copyright (C) 2012-2015  Pupil Labs

 Distributed under the terms of the CC BY-NC-SA License.
 License details are in the file license.txt, distributed as part of this software.
----------------------------------------------------------------------------------~(*)
'''

import os, sys, platform
import getpass
import cv2
from pyglui import ui
import numpy as np
from scipy.interpolate import UnivariateSpline
from plugin import Plugin
from time import strftime,localtime,time,gmtime
from shutil import copy2
from glob import glob
from audio import Audio_Capture,Audio_Input_Dict
from file_methods import save_object
from av_writer import JPEG_Writer
from cv2_writer import CV_Writer
# koosha
import zbar
#import webbrowser 
# koosha ends
#logging
import logging

logger = logging.getLogger(__name__)

import subprocess as sp

def get_auto_name():
    return strftime("%Y_%m_%d", localtime())

def sanitize_timestamps(ts):
    logger.debug("Checking %s timestamps for monotony in direction and smoothness"%ts.shape[0])
    avg_frame_time = (ts[-1] - ts[0])/ts.shape[0]
    logger.debug('average_frame_time: %s'%(1./avg_frame_time))

    raw_ts = ts #only needed for visualization
    runs = 0
    while True:
        #forward check for non monotonic increasing behaviour
        clean = np.ones((ts.shape[0]),dtype=np.bool)
        damper  = 0
        for idx in range(ts.shape[0]-1):
            if ts[idx] >= ts[idx+1]: #not monotonically increasing timestamp
                damper = 50
            clean[idx] = damper <= 0
            damper -=1

        #backward check to smooth timejumps forward
        damper  = 0
        for idx in range(ts.shape[0]-1)[::-1]:
            if ts[idx+1]-ts[idx]>1: #more than one second forward jump
                damper = 50
            clean[idx] &= damper <= 0
            damper -=1

        if clean.all() == True:
            if runs >0:
                logger.debug("Timestamps were bad but are ok now. Correction runs: %s"%runs)
                # from matplotlib import pyplot as plt
                # plt.plot(frames,raw_ts)
                # plt.plot(frames,ts)
                # # plt.scatter(frames[~clean],ts[~clean])
                # plt.show()
            else:
                logger.debug("Timestamps are clean.")
            return ts

        runs +=1
        if runs > 4:
            logger.error("Timestamps could not be fixed!")
            return ts

        logger.warning("Timestamps are not sane. We detected non monotitc or jumpy timestamps. Fixing them now")
        frames = np.arange(len(ts))
        s = UnivariateSpline(frames[clean],ts[clean],s=0)
        ts = s(frames)


class Recorder(Plugin):
    """Capture Recorder"""
    def __init__(self,g_pool,session_name = get_auto_name(),rec_dir=None, user_info={'name':'','additional_field':'change_me'},info_menu_conf={},show_info_menu=False, record_eye = False, audio_src = 'No Audio', raw_jpeg=False):
        super(Recorder, self).__init__(g_pool)
        
        self.states = ['start', 'want', 'pick']
        self.all_qr = {'termo1' : 75, 'light1' : False, 'light2' : False, 'light3': False}
        self.this_state = 'start'
        self.start_state = time()
        self.qr_codes = {}
        self.gaze_x = -1
        self.gaze_y = -1
        self.avg_col = 0
        self.choice = ""

        #update name if it was autogenerated.
        if session_name.startswith('20') and len(session_name)==10:
            session_name = get_auto_name()

        base_dir = self.g_pool.user_dir.rsplit(os.path.sep,1)[0]
        default_rec_dir = os.path.join(base_dir, 'recordings')

        if rec_dir and rec_dir != default_rec_dir and self.verify_path(rec_dir):
            self.rec_dir = rec_dir
        else:
            try:
                os.makedirs(default_rec_dir)
            except OSError as e:
                if 'File exists' in '%s'%e:
                    pass
                else:
                    logger.error("Could not create Rec dir")
                    raise e
            else:
                logger.info('Created standard Rec dir at "%s"'%default_rec_dir)
            self.rec_dir = default_rec_dir
        # if rec_dir and rec_dir != default_rec_dir and self.verify_path(rec_dir):
        #     self.rec_dir = rec_dir
        # else:
        #     #lets make a rec dir next to the user dir
        #     base_dir = self.g_pool.user_dir.rsplit(os.path.sep,1)[0]
        #     self.rec_dir = os.path.join(base_dir,'recordings')
        #     if not os.path.isdir(self.rec_dir):
        #         os.mkdir(self.rec_dir)

        self.raw_jpeg = raw_jpeg
        self.order = .9
        self.record_eye = record_eye
        self.session_name = session_name
        self.audio_devices_dict = Audio_Input_Dict()
        if audio_src in self.audio_devices_dict.keys():
            self.audio_src = audio_src
        else:
            self.audio_src = 'No Audio'
        self.running = False
        self.menu = None
        self.button = None

        self.user_info = user_info
        self.show_info_menu = show_info_menu
        self.info_menu = None
        self.info_menu_conf = info_menu_conf
        self.height, self.width = self.g_pool.capture.frame_size


    def get_init_dict(self):
        d = {}
        d['record_eye'] = self.record_eye
        d['audio_src'] = self.audio_src
        d['session_name'] = self.session_name
        d['user_info'] = self.user_info
        d['info_menu_conf'] = self.info_menu_conf
        d['show_info_menu'] = self.show_info_menu
        d['rec_dir'] = self.rec_dir
        d['raw_jpeg'] = self.raw_jpeg
        # if self.menu:
        #     d['menu_conf'] = self.menu.configuration
        # else:
        #     d['menu_conf'] = self.menu_conf
        return d


    def init_gui(self):
        self.menu = ui.Growing_Menu('Recorder')
        # self.menu.configuration = self.menu_conf
        self.g_pool.sidebar.insert(3,self.menu)
        self.menu.append(ui.Info_Text('Pupil recordings are saved like this: "path_to_recordings/recording_session_name/nnn" where "nnn" is an increasing number to avoid overwrites. You can use "/" in your session name to create subdirectories.'))
        self.menu.append(ui.Info_Text('Recordings are saved to "~/pupil_recordings". You can change the path here but note that invalid input will be ignored.'))
        self.menu.append(ui.Text_Input('rec_dir',self,setter=self.set_rec_dir,label='Path to recordings'))
        self.menu.append(ui.Text_Input('session_name',self,setter=self.set_session_name,label='Recording session name'))
        self.menu.append(ui.Switch('show_info_menu',self,on_val=True,off_val=False,label='Request additional user info'))
        self.menu.append(ui.Selector('raw_jpeg',self,selection = [True,False], labels=["bigger file, less CPU", "smaller file, more CPU"],label='compression'))
        self.menu.append(ui.Info_Text('Recording the raw eye video is optional. We use it for debugging.'))
        self.menu.append(ui.Switch('record_eye',self,on_val=True,off_val=False,label='Record eye'))
        self.menu.append(ui.Selector('audio_src',self, selection=self.audio_devices_dict.keys()))

        self.button = ui.Thumb('running',self,setter=self.toggle,label='QR',hotkey='r')
        self.button.on_color[:] = (1,.0,.0,.8)
        self.g_pool.quickbar.insert(1,self.button)


    def deinit_gui(self):
        if self.menu:
            # self.menu_conf = self.menu.configuration
            self.g_pool.sidebar.remove(self.menu)
            self.menu = None
        if self.button:
            self.g_pool.quickbar.remove(self.button)
            self.button = None

    def toggle(self, _=None):
        if self.running:
            self.stop()
        else:
            self.start()


    def get_rec_time_str(self):
        rec_time = gmtime(time()-self.start_time)
        return strftime("%H:%M:%S", rec_time)

    def start(self):
        self.timestamps = []
        # add self.data
        self.data = {'pupil_positions':[], 'gaze_positions': []}
        self.pupil_list = []
        self.gaze_list = []
        self.frame_count = 0
        self.running = True
        self.menu.read_only = True
        self.start_time = time()
        self.start_state = time()

        session = os.path.join(self.rec_dir, self.session_name)
        try:
            os.makedirs(session)
            logger.debug("Created new recordings session dir %s"%session)

        except:
            logger.debug("Recordings session dir %s already exists, using it." %session)

        # set up self incrementing folder within session folder
        counter = 0
        while True:
            self.rec_path = os.path.join(session, "%03d/" % counter)
            try:
                os.mkdir(self.rec_path)
                logger.debug("Created new recording dir %s"%self.rec_path)
                break
            except:
                logger.debug("We dont want to overwrite data, incrementing counter & trying to make new data folder")
                counter += 1

        self.meta_info_path = os.path.join(self.rec_path, "info.csv")

        with open(self.meta_info_path, 'w') as f:
            f.write("Recording Name\t"+self.session_name+ "\n")
            f.write("Start Date\t"+ strftime("%d.%m.%Y", localtime(self.start_time))+ "\n")
            f.write("Start Time\t"+ strftime("%H:%M:%S", localtime(self.start_time))+ "\n")


        if self.audio_src != 'No Audio':
            audio_path = os.path.join(self.rec_path, "world.wav")
            self.audio_writer = Audio_Capture(self.audio_devices_dict[self.audio_src],audio_path)
        else:
            self.audio_writer = None

        if self.raw_jpeg  and "uvc_capture" in str(self.g_pool.capture.__class__):
            self.video_path = os.path.join(self.rec_path, "world.mp4")
            self.writer = JPEG_Writer(self.video_path,int(self.g_pool.capture.frame_rate))
        # elif 1:
        #     self.writer = av_writer.AV_Writer(self.video_path)
        else:
            self.video_path = os.path.join(self.rec_path, "world.mkv")
            self.writer = CV_Writer(self.video_path, float(self.g_pool.capture.frame_rate), self.g_pool.capture.frame_size)
        # positions path to eye process
        if self.record_eye:
            for tx in self.g_pool.eye_tx:
                tx.send((self.rec_path,self.raw_jpeg))

        if self.show_info_menu:
            self.open_info_menu()
        # self.video_path = os.path.join(self.rec_path, "world.mkv")
        # self.writer = cv2.VideoWriter(self.video_path, int(cv2.cv.CV_FOURCC(*'DIVX')), float(self.g_pool.capture.frame_rate), (1280,720))
        # # positions path to eye process
        # if self.record_eye:
        #     for tx in self.g_pool.eye_tx:
        #         tx.send(self.rec_path)

        # if self.show_info_menu:
        #     self.open_info_menu()
###############
    def open_info_menu(self):
        self.info_menu = ui.Growing_Menu('additional Recording Info',size=(300,300),pos=(300,300))
        self.info_menu.configuration = self.info_menu_conf

        def populate_info_menu():
            self.info_menu.elements[:-2] = []
            for name in self.user_info.iterkeys():
                self.info_menu.insert(0,ui.Text_Input(name,self.user_info))

        def set_user_info(new_string):
            self.user_info = new_string
            populate_info_menu()

        populate_info_menu()
        self.info_menu.append(ui.Info_Text('Use the *user info* field to add/remove additional fields and their values. The format must be a valid Python dictionary. For example -- {"key":"value"}. You can add as many fields as you require. Your custom fields will be saved for your next session.'))
        self.info_menu.append(ui.Text_Input('user_info',self,setter=set_user_info,label="User info"))
        self.g_pool.gui.append(self.info_menu)

    def close_info_menu(self):
        if self.info_menu:
            self.info_menu_conf = self.info_menu.configuration
            self.g_pool.gui.remove(self.info_menu)
            self.info_menu = None

    def update(self,frame,events):
        if self.running:
            # update data
            self.data['pupil_positions'] += events['pupil_positions']
            self.data['gaze_positions'] += events['gaze_positions']

            #cv2.putText(frame.img, "Frame %s"%self.frame_count,(200,200), cv2.FONT_HERSHEY_SIMPLEX,1,(255,100,100)
            cv2.putText(frame.img, self.this_state,(200,100), cv2.FONT_HERSHEY_SIMPLEX,1,(255,100,100))
            if self.this_state == 'pick':
                cv2.putText(frame.img, self.choice,(300,100), cv2.FONT_HERSHEY_SIMPLEX,1,(100,100,255))
            QRs = qr_detect(frame)              
            for p in events['pupil_positions']:
                pupil_pos = p['timestamp'],p['confidence'],p['id'],p['norm_pos'][0],p['norm_pos'][1],p['diameter']

                self.pupil_list.append(pupil_pos)

            c = 0
            avg_x = -1
            avg_y = -1
            for g in events.get('gaze_positions',[]):
                gaze_pos = g['timestamp'],g['confidence'],g['norm_pos'][0],g['norm_pos'][1]
                avg_x = (avg_x * c + g['norm_pos'][0]) / (c+1)
                avg_y = (avg_y * c + g['norm_pos'][0]) / (c+1)
                c = c + 1
                self.gaze_list.append(gaze_pos)
                
            self.timestamps.append(frame.timestamp)

            avg_col = 0    

            self.stateT(frame, QRs, avg_x, avg_y, avg_col) 

            self.writer.write_video_frame(frame)
            self.frame_count += 1

            self.button.status_text = self.get_rec_time_str()

    def stop(self):
        #explicit release of VideoWriter
        self.writer.release()
        self.writer = None

        if self.record_eye:
            for tx in self.g_pool.eye_tx:
                try:
                    tx.send(None)
                except:
                    logger.warning("Could not stop eye-recording. Please report this bug!")

        save_object(self.data, os.path.join(self.rec_path, "pupil_data"))
        
        gaze_list_path = os.path.join(self.rec_path, "gaze_positions.npy")
        np.save(gaze_list_path,np.asarray(self.gaze_list))

        pupil_list_path = os.path.join(self.rec_path, "pupil_positions.npy")
        np.save(pupil_list_path,np.asarray(self.pupil_list))

        timestamps_path = os.path.join(self.rec_path, "world_timestamps.npy")
        ts = sanitize_timestamps(np.array(self.timestamps))
        np.save(timestamps_path,ts)

        try:
            copy2(os.path.join(self.g_pool.user_dir,"surface_definitions"),os.path.join(self.rec_path,"surface_definitions"))
        except:
            logger.info("No surface_definitions data found. You may want this if you do marker tracking.")

        try:
            copy2(os.path.join(self.g_pool.user_dir,"cal_pt_cloud.npy"),os.path.join(self.rec_path,"cal_pt_cloud.npy"))
        except:
            logger.warning("No calibration data found. Please calibrate first.")

        try:
            copy2(os.path.join(self.g_pool.user_dir,"camera_matrix.npy"),os.path.join(self.rec_path,"camera_matrix.npy"))
            copy2(os.path.join(self.g_pool.user_dir,"dist_coefs.npy"),os.path.join(self.rec_path,"dist_coefs.npy"))
        except:
            logger.info("No camera intrinsics found.")

        try:
            with open(self.meta_info_path, 'a') as f:
                f.write("Duration Time\t"+ self.get_rec_time_str()+ "\n")
                if self.g_pool.binocular:
                    f.write("Eye Mode\tbinocular\n")
                else:
                    f.write("Eye Mode\tmonocular\n")
                f.write("Duration Time\t"+ self.get_rec_time_str()+ "\n")
                f.write("World Camera Frames\t"+ str(self.frame_count)+ "\n")
                f.write("World Camera Resolution\t"+ str(self.width)+"x"+str(self.height)+"\n")
                f.write("Capture Software Version\t%s\n"%self.g_pool.version)
                if platform.system() == "Windows":
                    username = os.environ["USERNAME"]
                    sysname, nodename, release, version, machine, _ = platform.uname()
                else:
                    username = os.getlogin()
                    try:
                        sysname, nodename, release, version, machine = os.uname()
                    except:
                        sysname, nodename, release, version, machine = sys.platform,None,None,None,None
                f.write("User\t"+username+"\n")
                f.write("Platform\t"+sysname+"\n")
                f.write("Machine\t"+nodename+"\n")
                f.write("Release\t"+release+"\n")
                f.write("Version\t"+version+"\n")
        except Exception:
            logger.exception("Could not save metadata. Please report this bug!")

        try:
            with open(os.path.join(self.rec_path, "user_info.csv"), 'w') as f:
                for name,val in self.user_info.iteritems():
                    f.write("%s\t%s\n"%(name,val))
        except Exception:
            logger.exception("Could not save userdata. Please report this bug!")


        self.close_info_menu()

        if self.audio_writer:
            self.audio_writer = None

        self.running = False
        self.menu.read_only = False
        self.button.status_text = ''



    def cleanup(self):
        """gets called when the plugin get terminated.
           either volunatily or forced.
        """
        if self.running:
            self.stop()
        self.deinit_gui()

    def verify_path(self, val):
        try:
            n_path = os.path.expanduser(val)
            logger.debug("Expanded user path.")
        except:
            n_path = val
        if not n_path:
            logger.waring("Please specify a path.")
            return False
        elif not os.path.isdir(n_path):
            logger.warning("This is not a valid path.")
            return False
        elif not writable_dir(n_path):
            logger.warning("Do not have write access to '%s'." %n_path)
            return False
        else:
            return n_path


    def set_rec_dir(self,val):
        # try:
        #     n_path = os.path.expanduser(val)
        #     logger.debug("Expanded user path.")
        # except:
        #     n_path = val
        # if not n_path:
        #     logger.warning("Please specify a path.")
        # elif not os.path.isdir(n_path):
        #     logger.warning("This is not a valid path.")
        # # elif not os.access(n_path, os.W_OK):
        # elif not writable_dir(n_path):
        #     logger.warning("Do not have write access to '%s'."%n_path)
        # else:
        #     self.rec_dir = n_path
        n_path = self.verify_path(val)
        if n_path:
            self.rec_dir = n_path

    def set_session_name(self, val):
        if not val:
            self.session_name = get_auto_name()
        else:
            if '/' in val:
                logger.warning('You session name with create one or more subdirectories')
            self.session_name = val

    def stateT(self, frame, QRs, agaze_x, agaze_y, avg_col):
        if time() - self.start_time < 1:
            self.gaze_x = agaze_x
            self.gaze_y = agaze_y
        
        if self.this_state == 'start':
            if time() - self.start_state < 2:
                self.qr_codes = {}
                return True
            if self.Nochange_gaze(agaze_x, agaze_y, avg_col):
                    self.this_state = 'want'
            
            self.start_state = time()        
            self.gaze_x = agaze_x
            self.gaze_y = agaze_y
            return True
        
        if self.this_state == 'want':
            if time() - self.start_state < 2:
                self.qr_codes.update(QRs)
                return True    
            if self.Nochange_gaze(agaze_x, agaze_y, avg_col) and self.qr_codes:
                self.this_state = 'pick'
                self.choice = self.choose(frame)
                #print self.qr_codes, self.choice
            else: 
                
                self.this_state = 'start'        
            self.start_state = time()
            self.gaze_x = agaze_x
            self.gaze_y = agaze_y
            return True
        if self.this_state == 'pick':
            if time() - self.start_state < 4:
                return True
            if self.Nochange_gaze(agaze_x, agaze_y, avg_col):
                    del self.qr_codes[self.choice]
                    if not self.qr_codes:
                        self.this_state = 'start'
                    else:
                        self.choice = self.choose(frame)
                        #print self.choice
            else:
                print self.choice, " chosen"
                
                self.this_state = 'start'    
            self.start_state = time()
            self.gaze_x = agaze_x
            self.gaze_y = agaze_y
        
        return True

    def Nochange_gaze(self, agaze_x, agaze_y, avg_col):
        #print self.gaze_x, self.gaze_y, agaze_x, agaze_y
        return (abs(self.gaze_x - agaze_x) < .1 and abs(self.gaze_y - agaze_y) < .1)

    def choose(self, frame):
        h = frame.img.shape[0]
        w = frame.img.shape[1]
        gaze_x = self.gaze_x
        gaze_y = self.gaze_y
        if (gaze_x < 0 or gaze_y < 0):
            gaze_x = .5
            gaz_y = .5
        k = self.qr_codes.keys()
        chosen = k[0]
        min_dist = 1000000
        for name, mc in self.qr_codes.items():
            dis = (mc[0] - gaze_x * w) * (mc[0] - gaze_x * w) + (mc[1] - gaze_y * h) * (mc[1] - gaze_y * h)
            if dis < min_dist:
                min_dist = dis
                chosen = name
        return chosen   

def writable_dir(n_path):
    try:
         open(os.path.join(n_path,'dummpy_tmp'), 'w')
    except IOError:
         return False
    else:
         os.remove(os.path.join(n_path,'dummpy_tmp'))
         return True

def qr_detect(frame):
    QR_codes_list = {} # list of QR codes in the screen
    num = 0
    standard_QR = np.float32([[74.014, 74.014], [74.014, 214.014] , [214.014, 74.014]]) #top, bottom, right
    st_size = (290, 290)
    eps3 = 5; #for size check
    imgray = cv2.cvtColor(frame.img,cv2.COLOR_BGR2GRAY)
    ret,thresh = cv2.threshold(imgray,127,255,0)
    contours,hierarchy = cv2.findContours(thresh, cv2.RETR_TREE,cv2.CHAIN_APPROX_SIMPLE)
    if len(contours) < 1 or len(hierarchy) < 1:
        return []
    hierarchy = hierarchy[0]


    # find small squares:
    squares = []
    for component in zip(contours, hierarchy):
        currentContour = component[0]
        currentHierarchy = component[1]
        c = 0
        while currentHierarchy[2] > 0:
            currentHierarchy = hierarchy[currentHierarchy[2]]
            c = c + 1
        if c > 1:    
            squares.append(currentContour)  

    mc = []
    for i in range(len(squares)):
        M = cv2.moments(squares[i])
        mc.append([int(M['m10']/M['m00']), int(M['m01']/M['m00'])])

    #matching process:
    while (len(squares) > 2):
        A = 0
        # sort based on distance to A
        for B in range(1, len(squares) - 1):
            for C in range(B+1, len(squares)):
                AB = ((mc[A][0]- mc[B][0]) ** 2) + ((mc[A][1]- mc[B][1]) ** 2) 
                CA = ((mc[A][0]- mc[C][0]) ** 2) + ((mc[A][1]- mc[C][1]) ** 2)    
                if (AB > CA):
                    mc[B], mc[C] = mc[C], mc[B]
                    squares[B], squares[C] = squares[C], squares[B]
        
        # match squares together:
        found = False
        for B in range(1, len(squares) -1):
            for C in range(B+1, len(squares)):
                if max(cv2.contourArea(squares[A]), cv2.contourArea(squares[B]), cv2.contourArea(squares[C])) / min(cv2.contourArea(squares[A]), cv2.contourArea(squares[B]), cv2.contourArea(squares[C])) < eps3:
                    bottom = max(mc[A][1], mc[B][1], mc[C][1])
                    right = max (mc[A][0], mc[B][0], mc[C][0])
                    if (mc[A][0] < right and mc[A][1] < bottom):
                        to = A
                    else:
                        if (mc[B][0] < right and mc[B][1] < bottom):
                            to = B
                        else:
                            to = C

                    if (mc[A][0] == right and mc[A][1] < bottom):
                        ri = A
                    else:
                        if (mc[B][0] == right and mc[B][1] < bottom):
                            ri = B
                        else:
                            ri = C

                    if (mc[A][0] < right and mc[A][1] == bottom):
                        bt = A
                    else:
                        if (mc[B][0] < right and mc[B][1] == bottom):
                            bt = B
                        else:
                            bt = C            
                    if (to != ri and to != bt and ri != bt):
                        #print mc[A], mc[B], mc[C]
                        found = True
                        num = num + 1
                        break
            if found:
                break
        if found:
            cv2.drawContours(frame.img, squares, to, (255, 0, 0), 3)
            cv2.drawContours(frame.img, squares, bt, (0, 255, 0), 3)
            cv2.drawContours(frame.img, squares, ri, (255, 0, 255, 0), 3)
            area = 4 * max(cv2.contourArea(squares[A]), cv2.contourArea(squares[B]), cv2.contourArea(squares[C]))
            top_l_x = max(int(mc[to][0] - area **.5), 0)
            top_l_y = max(int(mc[to][1] - area **.5), 0)
            area = cv2.contourArea(squares[ri])
            bottom_r_x = int(.5 + mc[ri][0] +  area ** .5)
            bottom_r_y = int(.5 + mc[bt][1] + area ** .5)
            #cv2.rectangle(frame.img,(top_l_x, top_l_y), (bottom_r_x, bottom_r_y),(0,255,0),1)
            crop_img = imgray[top_l_y: bottom_r_y, top_l_x: bottom_r_x]
            this_QR = np.float32([[mc[to][0] - top_l_x, mc[to][1] - top_l_y], [mc[bt][0] - top_l_x, mc[bt][1] - top_l_y], [mc[ri][0] - top_l_x, mc[ri][1] - top_l_y]])
            M = cv2.getAffineTransform(this_QR, standard_QR)
            st_img = cv2.warpAffine(crop_img, M, st_size)
            #cv2.imshow("QRCODE" + str(num), st_img)
            qr_code = read_QR(st_img)
            if qr_code != 'None':
                QR_codes_list[qr_code] = [(top_l_x + bottom_r_x) /2, (top_l_y + bottom_r_y) /2]
            cv2.putText(frame.img, qr_code,(top_l_x,top_l_y), cv2.FONT_HERSHEY_SIMPLEX,1,(255,100,100)) 
            squares.pop(C)
            squares.pop(B)
            mc.pop(C)
            mc.pop(B)
        squares.pop(A)
        mc.pop(A)
    return QR_codes_list

def read_QR(imgray):
    raw = str(imgray.data)
    scanner = zbar.ImageScanner()
    scanner.parse_config('enable')
    imageZbar = zbar.Image( imgray.shape[0], imgray.shape[1],  'Y800', raw)
    scanner.scan(imageZbar)
    for symbol in imageZbar:
        return symbol.data
    return 'None'



