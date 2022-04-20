from ast import excepthandler
import os
from tempfile import TemporaryFile
from turtle import RawTurtle
from typing import Dict
from typing import Any
from typing import List
from typing import Union

import BASIC.message as msg
import BASIC.compute as comp
import BASIC.utils as ut
from matplotlib.cbook import strip_math

import numpy as np

from gpaw import restart

from ase.calculators.calculator import kptdensity2monkhorstpack as kdens2mp
from ase.parallel import paropen, parprint, world, barrier
from ase.io import read
from ase.db import connect

from glob import glob

import math

import re


# def detect_cluster(slab,tol=0.3):
#     n=len(slab)
#     dist_matrix=np.zeros((n, n))
#     slab_c=np.sort(slab.get_positions()[:,2])
#     for i, j in itertools.combinations(list(range(n)), 2):
#         if i != j:
#             cdist = np.abs(slab_c[i] - slab_c[j])
#             dist_matrix[i, j] = cdist
#             dist_matrix[j, i] = cdist
#     condensed_m = squareform(dist_matrix)
#     z = linkage(condensed_m)
#     clusters = fcluster(z, tol, criterion="distance")
#     return slab_c,list(clusters)


class surf_calc_conv:
    def __init__(self,
                element: str,
                miller_index: str,
                shift: float,
                order: int,
                gpaw_calc,
                rela_tol: float=0.015,
                restart_calc: bool=False,
                fix_layer: int=2,
                vacuum: int=10,
                solver_fmax: float=0.01,
                solver_max_step: float=0.05,
                surf_energy_calc_mode: str='regular',
                fix_option: str='bottom'):
        #intialize
        ##globalize variables
        self.element=element
        self.solver_max_step=solver_max_step
        self.solver_fmax=solver_fmax
        self.surf_energy_calc_mode=surf_energy_calc_mode
        self.vacuum=vacuum
        self.fix_option=fix_option
        self.fix_layer=fix_layer
        self.miller_index_tight=''.join(miller_index.split(','))
        self.miller_index_loose=tuple(map(int,miller_index.split(','))) #tuple
        self.shift=shift
        self.order=order
        self.gpaw_calc=gpaw_calc
        self.final_slab_name=self.element+'_'+self.miller_index_tight+'_'+str(self.shift)+'_'+str(order)
        self.raw_slab_dir='results/'+element+'/'+'raw_surf/'
        self.target_dir='results/'+element+'/'+'surf/'
        self.target_sub_dir=self.target_dir+self.miller_index_tight+'_'+str(self.shift)+'_'+str(order)+'/'
        self.report_location=(self.target_dir+self.miller_index_tight+'_'+str(self.shift)+'_'+str(order)+'_results_report.txt')
        self.rela_tol = rela_tol

        ##connect to optimize bulk database to get gpw_dir and bulk potential_energy
        db_bulk=connect('final_database/bulk.db')
        kdensity=db_bulk.get(name=self.element).kdensity
        self.bulk_potential_energy=(db_bulk.get_atoms(name=self.element).get_potential_energy())/len(db_bulk.get_atoms(name=element))
        
        ##read the smallest slab to get the kpoints
        self.ascend_all_cif_files_full_path=self.sort_raw_slab()

        raw_slab_smallest=read(self.ascend_all_cif_files_full_path[0])
        raw_slab_smallest.pbc=[1,1,0]
        kpts=kdens2mp(raw_slab_smallest,kptdensity=kdensity,even=True)
        self.gpaw_calc.__dict__['parameters']['kpts']=kpts
        self.calc_dict=self.gpaw_calc.__dict__['parameters']

        ##generate report
        if self.calc_dict['spinpol']:
            self.init_magmom=0#np.mean(db_bulk.get_atoms(name=element).get_magnetic_moments())
        self.initialize_report()


        # convergence test 

        ## number of layers
        ### restart 
        if restart_calc and len(glob(self.target_sub_dir+'*/slab.gpw'))>=1:
            ascend_layer_ls,ascend_gpw_files_dir=self.gather_gpw_file()
            diff_primary=100
            diff_second=100
            if len(ascend_gpw_files_dir) > 2:
                for i in range((len(ascend_layer_ls)-3)+1):
                    self.convergence_update(i,ascend_gpw_files_dir)
                    diff_primary=max(self.surf_energies_diff_arr[0],self.surf_energies_diff_arr[2])
                    diff_second=self.surf_energies_diff_arr[1]
        else:
            #os.remove(self.target_dir+self.miller_index_tight+'_'+str(self.shift)+'/'+)
            ascend_layer_ls=[]
            diff_primary=100
            diff_second=100
        iters=len(ascend_layer_ls)
        self.convergence_loop(iters,diff_primary,diff_second)

        #finalize
        ascend_gpw_files_dir=self.gather_gpw_file()[1]
        ## calculate the surface energy
        if self.surf_energy_calc_mode == 'regular':
            final_atoms,self.gpaw_calc=restart(ascend_gpw_files_dir[-3])
            slab_energy=[final_atoms.get_potential_energy()]
            surface_area=[2*final_atoms.cell[0][0]*final_atoms.cell[1][1]]
            num_of_atoms=[len(final_atoms)]
            surf_energy=np.round(self.surface_energy_calculator(np.array(slab_energy),np.array(surface_area),np.array(num_of_atoms))[0],decimals=4)
            self.calc_dict=self.gpaw_calc.__dict__['parameters']
        elif self.surf_energy_calc_mode == 'linear-fit':
            slab_energy_lst=[]
            surface_area_total_lst=[]
            num_of_atoms_lst=[]
            for gpw_file_dir in ascend_gpw_files_dir[-3:]:
                interm_atoms=restart(gpw_file_dir)[0]
                slab_energy_lst.append(interm_atoms.get_potential_energy())
                surface_area_total_lst.append(2*interm_atoms.cell[0][0]*interm_atoms.cell[1][1])
                num_of_atoms_lst.append(len(interm_atoms))
            surf_energy=np.round(self.surface_energy_calculator(np.array(slab_energy_lst),np.array(surface_area_total_lst),np.array(num_of_atoms_lst))[0],decimals=4)
            final_atoms,self.gpaw_calc=restart(ascend_gpw_files_dir[-3])
            self.calc_dict=self.gpaw_calc.__dict__['parameters']
        else:
            raise RuntimeError(self.surf_energy_calc_mode+'mode not avilable. Available modes are regular, linear-fit.')
        
        ##save to database
        db_slab_interm=connect(self.target_dir+'all_miller_indices_all_shift'+'.db')
        id=db_slab_interm.reserve(name=self.final_slab_name)
        if id is None:
            id=db_slab_interm.get(name=self.final_slab_name).id
            db_slab_interm.update(id=id,atoms=final_atoms,name=self.final_slab_name,
                                    surf_energy=surf_energy,
                                    kpts=str(','.join(map(str, self.calc_dict['kpts']))))
        else:
            db_slab_interm.write(final_atoms,id=id,name=self.final_slab_name,
                                    surf_energy=surf_energy,
                                    kpts=str(','.join(map(str, self.calc_dict['kpts']))))
        f = paropen(self.report_location,'a')
        parprint('Surface energy calculation complete.', file=f)
        f.close()

    def convergence_loop(self,iters,diff_p,diff_s):
        while (diff_p>self.rela_tol or diff_s>self.rela_tol) and iters <= 6:
            layer=self.ascend_all_cif_files_full_path[iters].split('/')[-1].split('.')[0]
            location=self.target_sub_dir+layer+'x1x1'
            if os.path.isfile(location+'/'+'slab_interm.gpw'):
                slab, gpaw_calc = restart(location+'/'+'slab_interm.gpw')
            else:
                slab=read(self.ascend_all_cif_files_full_path[iters])
                pbc_checker(slab)
                slab.center(vacuum=self.vacuum,axis=2)
                if self.calc_dict['spinpol']:
                    slab.set_initial_magnetic_moments(self.init_magmom*np.ones(len(slab)))
                slab_c_coord,cluster=detect_cluster(slab)
                if self.fix_option == 'bottom':
                    unique_cluster_index=sorted(set(cluster), key=cluster.index)[self.fix_layer-1]
                    max_height_fix=max(slab_c_coord[cluster==unique_cluster_index])
                    fix_mask=slab.positions[:,2]<(max_height_fix+0.05) #add 0.05 Ang to make sure all bottom fixed
                else:
                    raise RuntimeError('Only bottom fix option available now.')
                slab.set_constraint(FixAtoms(mask=fix_mask))
                slab.set_calculator(self.gpaw_calc)
            opt.relax(slab,location,fmax=self.solver_fmax,maxstep=self.solver_max_step)
            ascend_layer_ls,ascend_gpw_files_dir=self.gather_gpw_file()
            iters=len(ascend_layer_ls)
            if iters>2:
                iter=iters-3
                self.convergence_update(iter,ascend_gpw_files_dir)
                diff_p=max(self.surf_energies_diff_arr[0],self.surf_energies_diff_arr[2])
                diff_s=self.surf_energies_diff_arr[1]
        self.check_convergence(diff_p,diff_s,iters)
    
    def check_convergence(self,diff_p,diff_s,iters):
        if iters>=6:
            if diff_p>self.rela_tol or diff_s>self.rela_tol:
                f=paropen(self.report_location,'a')
                parprint("WARNING: Max iterations reached! layer convergence test failed.",file=f)
                parprint("Computation Suspended!",file=f)
                parprint(' ',file=f)
                f.close()
                sys.exit()
        else:
            f=paropen(self.report_location,'a')
            parprint("layer convergence test success!",file=f)
            parprint("="*44,file=f)
            parprint('\n',file=f)
            f.close() 

    def convergence_update(self,iter,gpw_files_dir):
        slab_energy_lst=[]
        num_of_atoms_lst=[]
        surface_area_total_lst=[]
        pymatgen_layer_ls=[]
        for i in range(iter,iter+3,1):
            atoms=restart(gpw_files_dir[i])[0]
            slab_energy_lst.append(atoms.get_potential_energy())
            surface_area_total_lst.append(2*atoms.cell[0][0]*atoms.cell[1][1])
            num_of_atoms_lst.append(len(atoms))
            pymatgen_layer_ls.append(int(gpw_files_dir[i].split('/')[-2].split('x')[0]))
        surf_energy_lst=self.surface_energy_calculator(np.array(slab_energy_lst),np.array(surface_area_total_lst),np.array(num_of_atoms_lst))
        surf_energy_arr=np.array(surf_energy_lst)
        surf_energy_arr_rep= np.array((surf_energy_lst+surf_energy_lst)[1:4])
        self.surf_energies_diff_arr=np.round(np.abs(surf_energy_arr-surf_energy_arr_rep),decimals=4)
        self.convergence_update_report(pymatgen_layer_ls)


    def convergence_update_report(self,layer_ls):
        f = paropen(self.report_location,'a')
        parprint('Optimizing parameter: '+'layers',file=f)
        param_val_str='1st: '+str(layer_ls[0])+' 2nd: '+str(layer_ls[1])+' 3rd: '+str(layer_ls[2])
        parprint('\t'+param_val_str,file=f)
        divider_str='-'
        parprint('\t'+divider_str*len(param_val_str),file=f)
        substrat_str='| '+'2nd-1st'+' | '+'3rd-2nd'+' | '+'3rd-1st'+' |'
        parprint('\t'+substrat_str,file=f)
        energies_str='\t'+'| '
        for i in range(3):
            energies_str+=str(self.surf_energies_diff_arr[i])+'  '+'|'+' '
        energies_str+='eV/Ang^2'
        parprint(energies_str,file=f)
        parprint(' ',file=f)
        f.close()

    def surface_energy_calculator(self,slab_energy,surface_area_total,num_of_atoms):
        if self.surf_energy_calc_mode=='regular':
            surf_energy_lst=(1/surface_area_total)*(slab_energy-num_of_atoms*self.bulk_potential_energy)
            # for slab_energy,surface_area,num_of_atoms in zip(slab_energies,surface_area_total_lst,num_of_atoms_lst):
            #     surf_energy=(1/surface_area)*(slab_energy-num_of_atoms*self.bulk_potential_energy)
            #     surf_energy_lst.append(surf_energy)
        elif self.surf_energy_calc_mode=='linear-fit': ## TO-DO: need to think about how to fit to all slab energies, right now this is localize fitting
            assert type(num_of_atoms)==type(np.array([1,2,3])), 'In linear-fit mode, the type of num_of_atoms variable must be numpy.ndarray'
            assert type(slab_energy)==type(np.array([1,2,3])), 'In linear-fit mode, the type of slab_energy variable must be numpy.ndarray'
            assert len(num_of_atoms)==3, 'In linear-fit mode, the size of num_of_atoms variable must be 3'
            assert len(slab_energy)==3, 'In linear-fit mode, the size of slab_energy variable must be 3'
            self.fitted_bulk_potential_energy=np.round(np.polyfit(num_of_atoms,slab_energy,1)[0],decimals=5)
            surf_energy_lst=(1/surface_area_total)*(slab_energy-num_of_atoms*self.fitted_bulk_potential_energy)
            # for slab_energy,surface_area,num_of_atoms in zip(slab_energies,surface_area_total_lst,num_of_atoms_lst):
            #     surf_energy=(1/surface_area)*(slab_energy-num_of_atoms*self.fitted_bulk_potential_energy)
            #     surf_energy_lst.append(surf_energy)
        else:
            raise RuntimeError(self.surf_energy_calc_mode+'mode not avilable. Available modes are regular, linear-fit.')
        return list(surf_energy_lst)

    def gather_gpw_file(self):
        #need to make sure there are no gpw files from previous run
        gpw_files_dir=glob(self.target_sub_dir+'*/slab.gpw')
        gpw_slab_size=[gpw_file.split('/')[-2] for gpw_file in gpw_files_dir]
        slab_layers=[int(i.split('x')[0]) for i in gpw_slab_size]
        ascend_order=np.argsort(slab_layers)
        ascend_gpw_files_dir=[gpw_files_dir[i] for i in ascend_order]
        ascend_param_ls=np.sort(slab_layers)
        return ascend_param_ls,ascend_gpw_files_dir
        
    def sort_raw_slab(self):
        cif_file_dir=self.raw_slab_dir+str(self.miller_index_tight)+'/'+str(self.shift)+'/'+str(self.order)
        all_cif_files_full_path=glob(cif_file_dir+'/'+'*'+'.cif')
        cif_files_name=[cif_file.split('/')[-1] for cif_file in all_cif_files_full_path]
        layers=[int(name.split('.')[0]) for name in cif_files_name]

        #layers=[int(name.split('-')[0]) for name in layers_and_shift]
        ascend_order=np.argsort(layers)
        ascend_all_cif_files_full_path=[all_cif_files_full_path[i] for i in ascend_order]
        return ascend_all_cif_files_full_path

    def initialize_report(self):
        if world.rank==0 and os.path.isfile(self.report_location):
            os.remove(self.report_location)
        f = paropen(self.report_location,'a')
        parprint('Initial Parameters:', file=f)
        parprint('\t'+'xc: '+self.calc_dict['xc'],file=f)
        parprint('\t'+'h: '+str(self.calc_dict['h']),file=f)
        parprint('\t'+'kpts: '+str(self.calc_dict['kpts']),file=f)
        parprint('\t'+'sw: '+str(self.calc_dict['occupations']),file=f)
        parprint('\t'+'spin polarized: '+str(self.calc_dict['spinpol']),file=f)
        if self.calc_dict['spinpol']:
            parprint('\t'+'magmom: '+str(self.init_magmom),file=f)
        parprint('\t'+'convergence tolerance: '+str(self.rela_tol)+'eV/Ang^2',file=f)
        parprint('\t'+'surface energy calculation mode: '+str(self.surf_energy_calc_mode),file=f)
        parprint('\t'+'fixed layers: '+str(self.fix_layer),file=f)
        parprint('\t'+'fixed option: '+str(self.fix_option),file=f)
        parprint(' \n',file=f)
        f.close()

class bulk_calc_conv:
    def __init__(self,
                element: str,
                calculator_setting,
                restart_calculation: bool,
                relative_tolerance: float=0.015, #eV/atom
                eos_step: float = 0.05,
                solver_maxstep: float = 0.05,
                solver_fmax: float = 0.03
                ):

        # generate report
        target_dir=os.path.join('results', element, 'bulk', 'convergence_test')
        report_path=os.path.join(target_dir,'results_report.txt')
        self.calculator_parameters=calculator_setting.parameters

        initialize_report(report_path,self.calculator_parameters,'convergence_test',relative_tolerance)

        # convergence test 

        ## h size 
        parameter='h'
        ### restart 
        if restart_calculation and len(glob(os.path.join(target_dir,f"'results_'{parameter}",'*.gpw')))>0:
            descend_param_ls,descend_gpw_files_dir=self.gather_gpw_file(param)
            if len(descend_gpw_files_dir) < 3:
                self.restart_report(param,descend_gpw_files_dir[-1])
                diff_primary=100
                diff_second=100
            else: 
                for i in range((len(descend_param_ls)-3)+1):
                    self.convergence_update(param,i,descend_gpw_files_dir)
                    diff_primary=max(self.energies_diff_mat[0],self.energies_diff_mat[2])
                    diff_second=self.energies_diff_mat[1]
            self.gpaw_calc.__dict__['parameters'][param]=np.round(descend_param_ls[-1]-0.02,decimals=2)
            self.calc_dict=self.gpaw_calc.__dict__['parameters']
        else:
            descend_param_ls=[]
            diff_primary=100
            diff_second=100
        ### convergence loop
        iters=len(descend_param_ls)
        self.convergence_loop(param,iters,diff_primary,diff_second)
        
        ## kpts size 
        param='kdens'
        ### restart 
        if restart_calc and len(glob(self.target_dir+'results_'+param+'/'+'*.gpw'))>1:
            descend_param_ls,descend_gpw_files_dir=self.gather_gpw_file(param)
            if len(descend_gpw_files_dir) < 3:
                self.restart_report(param,descend_gpw_files_dir[0])
                diff_primary=100
                diff_second=100
            else: 
                for i in range((len(descend_param_ls)-3)+1):
                    self.convergence_update(param,i,descend_gpw_files_dir)
                    diff_primary=max(self.energies_diff_mat[0],self.energies_diff_mat[2])
                    diff_second=self.energies_diff_mat[1]
                # atoms,calc=restart(descend_gpw_files_dir[0])
            atoms=bulk_builder(self.element)
            kpts=kdens2mp(atoms,kptdensity=descend_param_ls[0])
            new_kpts=kpts.copy()
            new_kdens=descend_param_ls[0].copy()
            while np.mean(kpts)==np.mean(new_kpts):
                new_kdens+=0.2
                new_kpts=kdens2mp(atoms,kptdensity=np.round(new_kdens,decimals=1))
            new_kdens_dict={'density':new_kdens,'even':True}
            self.gpaw_calc.__dict__['parameters']['kpts']=new_kdens_dict
            self.calc_dict=self.gpaw_calc.__dict__['parameters']
        else:
            ### skip the first calculation
            descend_gpw_files_dir=self.gather_gpw_file('h')[1]
            atoms, calc = restart(descend_gpw_files_dir[-3]) 
            self.gpaw_calc=calc
            self.calc_dict=self.gpaw_calc.__dict__['parameters']
            param_val=self.calc_dict['kpts']['density']
            opt.optimize_bulk(atoms,
                        step=self.solver_step,fmax=self.solver_fmax,
                        location=self.target_dir+'results_'+param,
                        extname=param_val)
            descend_param_ls=self.gather_gpw_file(param)[0]
            diff_primary=100
            diff_second=100
        ### convergence loop
        iters=len(descend_param_ls)
        self.convergence_loop(param,iters,diff_primary,diff_second)

        #finalize
        descend_gpw_files_dir=self.gather_gpw_file(param)[1]
        final_atoms, calc = restart(descend_gpw_files_dir[2])
        self.gpaw_calc=calc
        self.calc_dict=self.gpaw_calc.__dict__['parameters']
        if self.calc_dict['spinpol']:
            self.final_magmom=final_atoms.get_magnetic_moments()
        db_final=connect('final_database'+'/'+'bulk.db')
        id=db_final.reserve(name=element)
        if id is None:
            id=db_final.get(name=element).id
            db_final.update(id=id,atoms=final_atoms,name=element,
                            kdensity=self.calc_dict['kpts']['density'],
                            gpw_dir=descend_gpw_files_dir[2])
        else:
            db_final.write(final_atoms,id=id,name=element,
                            kdensity=self.calc_dict['kpts']['density'],
                            gpw_dir=descend_gpw_files_dir[2])
        self.final_report()
    
    def convergence_loop(self,param,iters,diff_p,diff_s):
        while (diff_p>self.rela_tol or diff_s>self.rela_tol) and iters <= 6:
            atoms=bulk_builder(self.element)
            if self.calc_dict['spinpol']:
                atoms.set_initial_magnetic_moments(self.init_magmom*np.ones(len(atoms)))
            atoms.set_calculator(self.gpaw_calc)
            if param == 'h':
                param_val=self.calc_dict[param]
            elif param == 'kdens':
                param_val=self.calc_dict['kpts']['density']
            opt.optimize_bulk(atoms,
                                step=self.solver_step,fmax=self.solver_fmax,
                                location=self.target_dir+'results_'+param,
                                extname=param_val)
            #convergence update
            descend_param_ls,descend_gpw_files_dir=self.gather_gpw_file(param)
            iters=len(descend_param_ls)
            if iters>2:
                iter=iters-3
                self.convergence_update(param,iter,descend_gpw_files_dir)
                diff_p=max(self.energies_diff_mat[0],self.energies_diff_mat[2])
                diff_s=self.energies_diff_mat[1]
            #update param
            if (diff_p>self.rela_tol or diff_s>self.rela_tol):
                if param == 'h':
                    self.gpaw_calc.__dict__['parameters'][param]=np.round(param_val-0.02,decimals=2)
                elif param == 'kdens':
                    atoms=bulk_builder(self.element)
                    kpts=kdens2mp(atoms,kptdensity=descend_param_ls[0])
                    new_kpts=kpts.copy()
                    new_kdens=descend_param_ls[0].copy()
                    while np.mean(kpts)==np.mean(new_kpts):
                        new_kdens+=0.2
                        new_kdens=np.round(new_kdens,decimals=1)
                        new_kpts=kdens2mp(atoms,kptdensity=new_kdens) #even=True
                    new_kdens_dict={'density':new_kdens,'even':True}
                    self.gpaw_calc.__dict__['parameters']['kpts']=new_kdens_dict
            else:
                continue
            self.calc_dict=self.gpaw_calc.__dict__['parameters']
        #check iteration
        self.check_convergence(diff_p,diff_s,iters,param)
    
    def check_convergence(self,diff_p,diff_s,iters,param):
        if iters>=6:
            if diff_p>self.rela_tol or diff_s>self.rela_tol:
                f=paropen(self.report_location,'a')
                parprint("WARNING: Max iterations reached! "+param+" convergence test failed.",file=f)
                parprint("Computation Suspended!",file=f)
                parprint(' ',file=f)
                f.close()
                sys.exit()
        else:
            f=paropen(self.report_location,'a')
            parprint(param+" convergence test success!",file=f)
            parprint("="*44,file=f)
            parprint('\n',file=f)
            f.close() 

    def gather_gpw_file(self,param):
        gpw_files_dir=glob(self.target_dir+'results_'+param+'/'+'*.gpw')
        gpw_files_name=[name.split('/')[-1] for name in gpw_files_dir]
        param_ls=[float(i.split('-')[-1][:-4]) for i in gpw_files_name]
        descend_order=np.argsort(param_ls)[::-1]
        descend_gpw_files_dir=[gpw_files_dir[i] for i in descend_order]
        descend_param_ls=np.sort(param_ls)[::-1]
        return descend_param_ls,descend_gpw_files_dir

    def convergence_update(self,param,iter,gpw_files_dir):
        energies=[]
        param_ls=[]
        if param == 'kdens':
            gpw_files_dir=gpw_files_dir[::-1]
        for i in range(iter,iter+3,1):
            atoms, calc = restart(gpw_files_dir[i])
            if param == 'kdens':
                kdens=calc.__dict__['parameters']['kpts']['density']
                param_ls.append(kdens)
            elif param == 'h':
                param_ls.append(calc.__dict__['parameters'][param])
            energies.append(atoms.get_potential_energy()/len(atoms)) #eV/atom
        energies_arr = np.array(energies)
        energies_arr_rep = np.array((energies+energies)[1:4])
        self.energies_diff_mat=np.round(np.abs(energies_arr-energies_arr_rep),decimals=4)
        self.convergence_update_report(param,param_ls)

    def convergence_update_report(self,param,param_ls):
        f = paropen(self.report_location,'a')
        parprint('Optimizing parameter: '+param,file=f)
        param_val_str='1st: '+str(param_ls[0])+' 2nd: '+str(param_ls[1])+' 3rd: '+str(param_ls[2])
        parprint('\t'+param_val_str,file=f)
        divider_str='-'
        parprint('\t'+divider_str*len(param_val_str),file=f)
        substrat_str='| '+'2nd-1st'+' | '+'3rd-2nd'+' | '+'3rd-1st'+' |'
        parprint('\t'+substrat_str,file=f)
        energies_str='\t'+'| '
        for i in range(3):
            energies_str+=str(self.energies_diff_mat[i])+'  '+'|'+' '
        energies_str+='eV/atom'
        parprint(energies_str,file=f)
        parprint(' ',file=f)
        f.close()

    def restart_report(self,param,updated_gpw):
        calc = restart(updated_gpw)[1]
        f = paropen(self.report_location,'a')
        parprint('Restarting '+param+' convergence test...',file=f)
        if param == 'kdens':
            parprint('\t'+'Last computation:'+'\t'+param+'='+str(calc.__dict__['parameters']['kpts']),file=f)
        elif param == 'h':
            parprint('\t'+'Last computation:'+'\t'+param+'='+str(calc.__dict__['parameters']['h']),file=f)
        parprint(' ',file=f)
        f.close()

    
    def final_report(self):
        f = paropen(self.report_location,'a')
        parprint('Final Parameters:', file=f)
        parprint('\t'+'xc: '+self.calc_dict['xc'],file=f)
        parprint('\t'+'h: '+str(self.calc_dict['h']),file=f)
        parprint('\t'+'kpts: '+str(self.calc_dict['kpts']),file=f)
        parprint('\t'+'sw: '+str(self.calc_dict['occupations']),file=f)
        parprint('\t'+'spin polarized: '+str(self.calc_dict['spinpol']),file=f)
        if self.calc_dict['spinpol']:
            parprint('\t'+'magmom: '+str(self.final_magmom),file=f)
        parprint(' ',file=f)
        f.close()

def size_converge(element: str,
                computation_type: str, #surface, ads
                computation_setting: Dict[str, Any],
                parameter_to_converge: List[str], #list of parameter available option: 'layer', 'area'
                restart_calculation: bool,
                calculator_setting,
                relative_tolerance: float = 0.015, #eV/atom
                ):
    """
    Size convergence test computation.

    Parameters
    ----------
    
    element (REQUIRED): 
        Chemical symbols and the materials project id of the bulk structures to be computed. E.g. Cu_mp-30

    computation_type (REQUIRED):
        The type of the computation performed. Avilable options are `surf`, `ads`
    
    computation_setting (REQUIRED):
        A dictionary contained details revalent to the computation.
        For surface, E.g. {'miller_plane':miller_plane_index, 'shift': shift_val, 'order': order_val, 'fix_layer': 2, 'fix_mode': 'bottom', 'surface_energy_calculation_mode': 'linear-fit'}. Required keys: `shift` and `order`. If not specified, default value will be used, which are shown as the example.
        For ads, E.g. {`miller_plane`:miller_plane_index, `shift`: shift_val, `order`: order_val, 'adatom': adatom, 'adatom_energy': adatom_energy}

    parameter_to_converge (REQUIRED):
        A list of parameter to do convergence test on. The convergence test will be performed as the order of the list.
        Available options are  'layer', 'area'

    restart_calculation (REQUIRED):
        Boolean to control whether to continue with previous computation. 
        If 'True', computation will continue with previous.
        If 'False', a new computation will start.
    
    obtain_calculator:
        Boolean to control whether to obtain calculator setting from convergence test
        Default is True.

    calculator_setting:
        Dictionary of calculator setting from ASE interface.
        Default is None, but if `obtain_calculator` == False, calculator_setting must not be empty.

    relative_tolerance:
        Relative tolerance for the convergence test. Default value is 0.015 eV/atom.
    """
    #check the computation_type
    if computation_type not in ['surf','ads']:
        raise RuntimeError(f"{computation_type} is not supported. Available options are 'surf', 'ads'")
    
    #prepare path and database
    elif computation_type == 'surf':
        slab_dir=os.path.join('results', element, computation_type, f"{computation_setting['miller_plane']}_{computation_setting['shift']}_{computation_setting['order']}")
        target_dir=os.path.join(slab_dir,'convergence_test','size')
    elif computation_type == 'ads':
        raise RuntimeError(f"{computation_type} not supported.")
    if world.rank==0 and not os.path.isdir(target_dir):
        os.makedirs(target_dir,exist_ok=True)
    barrier()
    if computation_type == 'surf':
        database_path=os.path.join('final_database',"surf_size.db")
        calc_database_path=os.path.join('final_database','surf_calc.db')
    if computation_type == 'ads':
        database_path=os.path.join('final_database',f"ads_{computation_setting['adatom']}_size.db")
        calc_database_path=os.path.join('final_database',f"ads_{computation_setting['adatom']}_calc.db")
    database=connect(database_path)
    calc_database=connect(calc_database_path)

    #call the object to prepare
    parameter_converge_obj=size_converge_loop(element,computation_type,computation_setting,parameter_to_converge,target_dir)

    for parameter in parameter_to_converge:
        #create report
        report_path = os.path.join(target_dir, f"{parameter}_report.txt")
        msg.initialize_report(report_path, calculator_setting.parameters, 'convergence_test', relative_tolerance=relative_tolerance)

        #access data in the database
        try:
            database.get(full_name=parameter_converge_obj.full_name)
            #database.get(full_name=parameter_converge_obj.full_name)
            entry_exist=True
        except:
            entry_exist=False

        if entry_exist: #entry exist
            #if this parameter has already be converged.
            converged_parameter_lst=database.get(full_name=parameter_converge_obj.full_name).converged_parameter.split(', ')
            #if converged, skip the calculation
            if parameter in converged_parameter_lst:
                #TO-DO: other sanity check 
                msg.write_message_in_report(report_path,f'Convergenced parameter found in database. {parameter} convergence test skip.')
            #otherwise do the calculation with previous size
            else:
                try:
                    calc_database.get(full_name=parameter_converge_obj.full_name)
                    calc_entry_exist=True
                except:
                    calc_entry_exist=False
                if calc_entry_exist:
                    calculator_setting_dict = calc_database.get(full_name=parameter_converge_obj.full_name).calculator_parameters
                    for keys, values in calculator_setting_dict.items():
                        calculator_setting.parameters[keys] = values
                    msg.write_message_in_report(report_path, "Found calculator convergence test result in database. Update calculator setting with converged setting.")
                    
                #call the function in the object to compute
                converged_gpw_file=parameter_converge_obj.convergence_loop(calculator_setting,parameter,report_path,restart_calculation,relative_tolerance)
                
                #finalize
                converged_parameter_str=', '.join(converged_parameter_lst+[parameter])
                final_atoms, final_calculator= restart(converged_gpw_file)
                id = database.get(full_name=parameter_converge_obj.full_name).id
                database.update(id=id,atoms=final_atoms,converged_parameter=converged_parameter_str)
                msg.final_report(report_path,final_calculator.parameters)
        else:
            #with id meaning the entry does not exist
            #call the function in the object to compute
            converged_gpw_file=parameter_converge_obj.convergence_loop(calculator_setting,parameter,report_path,restart_calculation,relative_tolerance)
            #finalize
            final_atoms, final_calculator = restart(converged_gpw_file)
            id=database.reserve(full_name=parameter_converge_obj.full_name)
            database.write(final_atoms,id=id, full_name=parameter_converge_obj.full_name, converged_parameter=parameter)
            msg.final_report(report_path,final_calculator.parameters)

class size_converge_loop:
    def __init__(self,
                element: str,
                computation_type: str,
                computation_setting: Dict[str, Any],
                parameter_to_converge: List[str],
                target_dir: str,
                ):
        if computation_type == 'surf':
            self.full_name = '_'.join([element,computation_setting['miller_plane'],computation_setting['shift'],computation_setting['order']])
            #self.atoms_fix = read(os.path.join('results', element, 'surf', '_'.join([computation_setting['miller_plane'],computation_setting['shift'],computation_setting['order']]),'input_slab',f"{target_dir.split('/')[-1]}","input.traj"))
        
        self.element = element
        self.computation_type = computation_type
        self.computation_setting = computation_setting
        self.target_dir = target_dir
        
        self.parameter_converge_dict ={}
        for parameter in parameter_to_converge:
            parameter_dir = os.path.join(self.target_dir, f"{parameter}")
            if world.rank==0 and not os.apth.isdir(parameter_dir):
                os.makedirs(parameter_dir,exist_ok=True)
            barrier()
            self.paramter_converge_dict[parameter]=self.assemble_dictionary(parameter)
        
    def assemble_dictionary(self,
                            parameter:str,
                            ):
        parameter_dir=os.path.join(self.target_dir,f"{parameter}")
        gpw_file_path=os.path.join(parameter_dir,'*','*_finish.gpw')
        traj_file_path=os.path.join(parameter_dir,'*','*_finish.traj')
        coarse_to_fine_parameter_converge_lst, coarse_to_fine_gpw_file_path_lst, coarse_to_fine_traj_file_path_lst=self.gather_converge_progress(parameter,gpw_file_path,traj_file_path)
        single_parameter_converge_dict={'parameter_dir':parameter_dir,
                                        'parameter_converge_lst':coarse_to_fine_parameter_converge_lst,
                                        'gpw_file_path_lst':coarse_to_fine_gpw_file_path_lst,
                                        'traj_file_path_lst':coarse_to_fine_traj_file_path_lst}
        return single_parameter_converge_dict
    
    def gather_converge_progress(
                                self,
                                parameter: str,
                                gpw_file_path: str,
                                traj_file_path: str,
                                ):
        """
        Gather convergence progress and sort the list from coarse-to-fine (parameters, gpw_file_path, traj_file_path)

        Parameters
        ----------

        parameter(REQUIRED):
            Parameter to converge on.

        gpw_file_path(REQUIRED):

        traj_file_path(REQUIRED):

        """
        gpw_file_path_lst=glob(gpw_file_path)
        traj_file_path_lst=glob(traj_file_path)
        parameter_lst=[path.split('/')[-2] for path in gpw_file_path_lst]
    

        if parameter == 'layer':
            parameter_converge_lst=[int(parameter) for parameter in parameter_lst]
        elif parameter == 'area':
            parameter_converge_lst=[int(parameter.split('x')[0]) for parameter in parameter_lst]
        
        small_to_large_order=np.argsort(parameter_converge_lst)
        small_to_large_parameter_converge_lst=[parameter_converge_lst[i] for i in small_to_large_order]
        small_to_large_gpw_file_path_lst=[gpw_file_path_lst[i] for i in small_to_large_order]
        small_to_large_traj_file_path_lst=[traj_file_path_lst[i] for i in small_to_large_order]
        return small_to_large_parameter_converge_lst, small_to_large_gpw_file_path_lst, small_to_large_traj_file_path_lst

    def convergence_loop(self,
                        calculator_setting,
                        parameter:str,
                        report_path:str,
                        restart_calculation:bool,
                        relative_tolerance:float,
                        ):
        """
        Convergence loop for calculator parameter convergence test.

        Parameters
        ----------
        calculator_setting (REQUIRED):   
            Dictionary of calculator setting from ASE interface.

        parameter (REQUIRED):
            parameter to do convergence test.

        report_path (REQUIRED):
            path to the report.
        
        restart_calculation (REQUIRED):
            Boolean to control whether to continue with previous computation. 
            If 'True', computation will continue with previous.
            If 'False', a new computation will start.
        
        relative_tolerance (REQUIRED):
            Relative tolerance for the convergence test. Default value is 0.015 eV/atom.
        """
        single_parameter_converge_dict=self.parameter_converge_dict[parameter]
        primary_energy_difference,secondary_energy_difference=math.inf, math.inf
        #restart
        if restart_calculation and len(single_parameter_converge_dict['gpw_file_path_lst'])>0:
            primary_energy_difference,secondary_energy_difference=self.convergence_update(parameter,single_parameter_converge_dict,report_path)

        #converge
        iters=len(single_parameter_converge_dict['parameter_converge_lst'])
        while (primary_energy_difference>relative_tolerance or secondary_energy_difference>relative_tolerance) and iters <= 6:
            comp.slab_compute(self.element, calculator_setting,parameter,self.computation_setting,restart_calculation,)
            iters,primary_energy_difference,secondary_energy_difference=self.results_analysis(parameter,report_path)

        #finish
        return self.parameter_converge_dict[parameter]['gpw_file_path_lst'][-3]
    
    def convergence_update(self,
                            parameter,
                            single_parameter_converge_dict,
                            report_path):
        if len(single_parameter_converge_dict['gpw_file_path_lst']) < 3:
            atoms = restart(single_parameter_converge_dict['gpw_file_path_lst'][-1])[0]
            primary_energy_difference = math.inf
            secondary_energy_difference = math.inf
        else:
            slab_energy_lst=[]
            num_of_atoms_lst=[]
            for traj_file_path in single_parameter_converge_dict['traj_file_path_lst']:
                atoms=read(traj_file_path)
                slab_energy_lst.append(atoms.get_potential_energy())
                num_of_atoms_lst.append(len(atoms))
            if self.computation_type == 'surf':
                surface_area = 2*atoms.cell[0][0]*atoms.cell[1][1]
                converged_energy_arr=comp.calculate_surface_energy(self.element,np.array(slab_energy_lst),surface_area,np.array(num_of_atoms_lst),single_parameter_converge_dict['surface_energy_calculation_mode'],report_path)
            
            converged_energy_arr = converged_energy_arr[-3:]
            converged_energy_arr_rep = np.array(list(converged_energy_arr)+list(converged_energy_arr)[1:4])
            energy_difference_array = np.round(np.abs(converged_energy_arr-converged_energy_arr_rep),decimals=4)
            secondary_energy_difference = energy_difference_array[1]
            primary_energy_difference = max(energy_difference_array[0],energy_difference_array[2])
            msg.convergence_update_report(parameter,single_parameter_converge_dict,report_path,energy_difference_array)
        return primary_energy_difference, secondary_energy_difference


def calculator_parameter_converge(element: str, #for bulk, full_name is the element name; for surface/ads, full_name is the element_shift_order name
                                computation_type: str, #bulk, surface, ads (str)
                                computation_setting: Dict[str, Any], #dictionary with all computation relavent setting: for bulk {'eos_step':,'solver_fmax':,'solver_maxstep'}; for surface {'shift':,'order':,'fix_layer':,'fix_mode':,'surface_energy_calculation_mode':,}
                                parameter_to_converge: List[str], #list of parameter available options: 'h','kpts','kpts_density','occupations_width'
                                calculator_setting, 
                                restart_calculation: bool,
                                relative_tolerance: float = 0.015, #eV/atom
                                ):
    """
    Calculator parameter convergence test computation.

    Parameters
    ----------
    
    element (REQUIRED): 
        Chemical symbols and the materials project id of the bulk structures to be computed. E.g. Cu_mp-30

    computation_type (REQUIRED):
        The type of the computation performed. Avilable options are `bulk`, `surf`, `ads`
    
    computation_setting (REQUIRED):
        A dictionary contained details revalent to the computation.
        For bulk, E.g. {`eos_step`: 0.05, `solver_fmax`: 0.03, `solver_maxstep`: 0.05}. If not specified, default value will be used, which are shown as the example.
        For surface, E.g. {`miller_plane`:miller_plane_index, `shift`: shift_val, `order`: order_val, `fix_layer`: 2, 'fix_mode': `bottom`}. Required keys: `shift` and `order`. If not specified, default value will be used, which are shown as the example.
        For ads, TO-DO!!!!

    parameter_to_converge (REQUIRED):
        A list of parameter to do convergence test on. The convergence test will be performed as the order of the list.
        Available options are  'h', 'kpts', 'kpts_density', 'occupation_width'
    
    calculator_setting (REQUIRED):
        Dictionary of calculator setting from ASE interface.
    
    restart_calculation (REQUIRED):
        Boolean to control whether to continue with previous computation. 
        If 'True', computation will continue with previous.
        If 'False', a new computation will start.
    
    relative_tolerance:
        Relative tolerance for the convergence test. Default value is 0.015 eV/atom.
    """
    #check the computation_type
    if computation_type not in ['bulk','surf','ads']:
        raise RuntimeError(f"{computation_type} is not supported. Available options are 'bulk', 'surf', 'ads'")

    #prepare path and database
    if computation_type == 'bulk':
        target_dir=os.path.join('results', element, computation_type, 'convergence_test','calculator_parameter')
    elif computation_type == 'surf':
        slab_dir=os.path.join('results', element, computation_type, f"{computation_setting['miller_plane']}_{computation_setting['shift']}_{computation_setting['order']}")
        target_dir=os.path.join(slab_dir,'convergence_test','calculator_parameter',str(computation_setting['layer']))
    elif computation_type == 'ads':
        raise RuntimeError(f"{computation_type} not supported.")
    if world.rank==0 and not os.path.isdir(target_dir):
        os.makedirs(target_dir,exist_ok=True)
    barrier()

    database_path=os.path.join('final_database',f"{computation_type}_calc.db")
    database=connect(database_path)

    #call the object to prepare
    parameter_converge_obj=calculator_parameter_converge_loop(element,computation_type,computation_setting,parameter_to_converge,target_dir)
    
    for parameter in parameter_to_converge:
        #create report
        report_path=os.path.join(target_dir,f"{parameter}_report.txt")
        msg.initialize_report(report_path, calculator_setting.parameters, 'convergence_test', relative_tolerance=relative_tolerance)

        #access data in the database
        try:
            database.get(full_name=parameter_converge_obj.full_name)
            entry_exist=True
        except:
            entry_exist=False
        if entry_exist: #entry exist
            #if this parameter has already be converged.
            converged_parameter_lst=database.get(full_name=parameter_converge_obj.full_name).converged_parameter.split(', ')
            #if converged, skip the calculation
            if parameter in converged_parameter_lst:
                #if the exchange correlation functional is the same
                msg_lst=[]
                if database.get(full_name=parameter_converge_obj.full_name).calculator_parameters['xc'] != calculator_setting.parameters.xc:
                    msg.append('Exchange-correlation functional is not the same! Convergence test terminated.')
                if len(msg_lst) != 0:
                    msg_prinout = '/nERROR: '.join(msg_lst)
                    msg.write_message_in_report(report_path,'ERROR: '+msg_prinout)
                    raise RuntimeError
                #TO-DO: other sanity check (want to check the number of atoms. But what is the best way?)
                msg.write_message_in_report(report_path,f'Convergenced parameter found in database. {parameter} convergence test skip.')
            #otherwise do the calculation with previous converged calculator setting
            else:
                #update the calculator parameters with the converged calculator setting in the database

                for converged_parameter in converged_parameter_lst:
                    calculator_setting.parameters[converged_parameter]=database.get(full_name=parameter_converge_obj.full_name).calculator_parameters[converged_parameter.split('_')[0]]
                    msg.write_message_in_report(report_path,f'{converged_parameter} updated. Converged value: {calculator_setting.parameters[converged_parameter]}')
                #call the function in the object to compute
                converged_gpw_file=parameter_converge_obj.convergence_loop(calculator_setting,parameter,report_path,restart_calculation,relative_tolerance)
                
                #finalize
                converged_parameter_str=', '.join(converged_parameter_lst+[parameter])
                final_atoms, final_calculator= restart(converged_gpw_file)
                id = database.get(full_name=parameter_converge_obj.full_name).id
                database.update(id=id,atoms=final_atoms,converged_parameter=converged_parameter_str)
                msg.final_report(report_path,final_calculator.parameters)
        else:
            #with id meaning the entry does not exist
            #call the function in the object to compute
            converged_gpw_file=parameter_converge_obj.convergence_loop(calculator_setting,parameter,report_path,restart_calculation,relative_tolerance)
            #finalize
            final_atoms, final_calculator = restart(converged_gpw_file)
            id=database.reserve(full_name=parameter_converge_obj.full_name)
            database.write(final_atoms,id=id, full_name=parameter_converge_obj.full_name, converged_parameter=parameter)
            msg.final_report(report_path,final_calculator.parameters)


        

class calculator_parameter_converge_loop:
    def __init__(self,
                element: str,
                computation_type: str,
                computation_setting: Dict[str, Any],
                parameter_to_converge: List[str],
                target_dir: str):
        """
        Convergence loop to handle calculator parameter convergence test.

        Parameters
        ----------
        
        element (REQUIRED): 
            Chemical symbols and the materials project id of the bulk structures to be computed. E.g. Cu_mp-30

        computation_type (REQUIRED):
            The type of the computation performed. Avilable options are `bulk`, `slab`
        
        computation_setting (REQUIRED):
            A dictionary contained details revalent to the computation.
            For bulk, E.g. {`eos_step`: 0.05, `solver_fmax`: 0.03, `solver_maxstep`: 0.05}. If not specified, default value will be used, which are shown as the example.
            For surface, E.g. {`miller_plane`:miller_plane_index, `shift`: shift_val, `order`: order_val, `fix_layer`: 2, 'fix_mode': `bottom`, `surface_energy_calculation_mode`: `linear-fit`}. Required keys: `shift` and `order`. If not specified, default value will be used, which are shown as the example.
            For ads, TO-DO!!!!

        parameter_to_converge (REQUIRED):
            A list of parameter to do convergence test on. The convergence test will be performed as the order of the list.
            Available options are  'h', 'kpts', 'kpts_density', 'occupation_width'

        restart_calculation (REQUIRED):
            Boolean to control whether to continue with previous computation. 
            If 'True', computation will continue with previous.
            If 'False', a new computation will start.
        
        target_dir (REQUIRED):
            Path to save the computation file.
        """

        #create full name of the computation
        if computation_type == 'bulk':
            self.full_name = element
            self.atoms_fix = read(os.path.join('orig_cif_data',self.element,'input.traj'))
        elif computation_type == 'surf':
            self.full_name = '_'.join([element,computation_setting['miller_plane'],computation_setting['shift'],computation_setting['order']])
            self.atoms_fix = read(os.path.join('results', element, 'surf', '_'.join([computation_setting['miller_plane'],computation_setting['shift'],computation_setting['order']]),'input_slab',f"{target_dir.split('/')[-1]}","input.traj"))
        elif computation_type == 'ads':
            raise RuntimeError(f"{computation_type} not supported.")

        self.element = element
        self.computation_type = computation_type
        self.computation_setting = computation_setting
        self.target_dir = target_dir
        

        #create parameter convergence dictionary
        self.parameter_converge_dict={}
        for parameter in parameter_to_converge:
            parameter_dir=os.path.join(self.target_dir,f"{parameter}")
            if world.rank==0 and not os.path.isdir(parameter_dir):
                os.makedirs(parameter_dir,exist_ok=True)
            barrier()
            self.parameter_converge_dict[parameter]=self.assemble_dictionary(parameter)

    def assemble_dictionary(self,
                            parameter: str,
                            ):
        """
        Assemble dictionary for parameter converge progress.

        Parameters
        ----------
        
        parameter (REQUIRED):
            parameter to be converged on.
        """
        parameter_dir=os.path.join(self.target_dir,f"{parameter}")
        gpw_file_path=os.path.join(parameter_dir,'*_finish.gpw')
        traj_file_path=os.path.join(parameter_dir,'*_finish.traj')
        coarse_to_fine_parameter_converge_lst, coarse_to_fine_gpw_file_path_lst, coarse_to_fine_traj_file_path_lst=self.gather_converge_progress(parameter,gpw_file_path,traj_file_path)
        single_parameter_converge_dict={'parameter_dir':parameter_dir,
                                        'parameter_converge_lst':coarse_to_fine_parameter_converge_lst,
                                        'gpw_file_path_lst':coarse_to_fine_gpw_file_path_lst,
                                        'traj_file_path_lst':coarse_to_fine_traj_file_path_lst}
        return single_parameter_converge_dict

    def convergence_loop(self,
                        calculator_setting,
                        parameter:str,
                        report_path:str,
                        restart_calculation:bool,
                        relative_tolerance:float,
                        ):
        """
        Convergence loop for calculator parameter convergence test.

        Parameters
        ----------
        calculator_setting (REQUIRED):   
            Dictionary of calculator setting from ASE interface.

        parameter (REQUIRED):
            parameter to do convergence test.

        report_path (REQUIRED):
            path to the report.
        
        restart_calculation (REQUIRED):
            Boolean to control whether to continue with previous computation. 
            If 'True', computation will continue with previous.
            If 'False', a new computation will start.
        
        relative_tolerance (REQUIRED):
            Relative tolerance for the convergence test. Default value is 0.015 eV/atom.
        """
        single_parameter_converge_dict=self.parameter_converge_dict[parameter]
        parameter_split=parameter.split('_')
        parameter_value_for_calculator=calculator_setting.parameters[parameter_split[0]]
        parameter_value_for_file_name=calculator_setting.parameters
        for i in parameter_split:
            parameter_value_for_file_name=parameter_value_for_file_name[i]
        primary_energy_difference,secondary_energy_difference=math.inf, math.inf
        #restart
        if restart_calculation and len(single_parameter_converge_dict['gpw_file_path_lst'])>0:
            primary_energy_difference,secondary_energy_difference=self.convergence_update(parameter,single_parameter_converge_dict,report_path)
            calculator_setting=restart(single_parameter_converge_dict['gpw_file_path_lst'][-1])[1]
            parameter_value_for_calculator=calculator_setting.parameters[parameter_split[0]]

        

        #converge
        iters=len(single_parameter_converge_dict['parameter_converge_lst'])
        while (primary_energy_difference>relative_tolerance or secondary_energy_difference>relative_tolerance) and iters <= 6:
            if iters != 0:
                parameter_value_for_calculator,parameter_value_for_file_name=self.parameter_update(parameter,parameter_value_for_calculator)
                calculator_setting.parameters[parameter_split[0]]=parameter_value_for_calculator

            if self.computation_type == 'bulk':
                comp.bulk_compute(self.element, 
                                        calculator_setting, 
                                        converge_parameter=(parameter,parameter_value_for_file_name), 
                                        target_dir=single_parameter_converge_dict['parameter_dir'],
                                        eos_step=self.computation_setting['eos_step'], 
                                        solver_maxstep=self.computation_setting['solver_maxstep'],
                                        solver_fmax=self.computation_setting['solver_fmax'],
                                        )
            elif self.computation_type in ['surf','ads']:
                comp.slab_compute(self.element,
                                calculator_setting,
                                converge_parameter=(parameter,parameter_value_for_file_name),
                                computation_setting=self.computation_setting,
                                restart_calculation=restart_calculation,
                                compute_dir=single_parameter_converge_dict['parameter_dir'],
                                solver_maxstep=self.computation_setting['solver_maxstep'],
                                solver_fmax=self.computation_setting['solver_fmax'],
                                )
            iters,primary_energy_difference,secondary_energy_difference=self.results_analysis(parameter,report_path)

        #finish
        return self.parameter_converge_dict[parameter]['gpw_file_path_lst'][-3]

    def parameter_update(self,
                        parameter: str,
                        parameter_value: Union[float,Dict],
                        ):
        """
        Calculator parameter update function.

        Parameters
        ----------
        
        parameter (REQUIRED):
            parameter to do convergence test.

        parameter_value (REQUIRED):
            value of the parameter.
        """

        if parameter == 'h':
            parameter_value-=0.02
            parameter_value=np.round(parameter_value,decimals=2)
            parameter_for_file_name=parameter_value.copy()

        elif parameter == 'kpts':
            parameter_value=tuple(np.array(parameter_value)+2)
            if self.computation_type in ['surf', 'ads']:
                parameter_value=parameter_value[:2]+(1,)
            parameter_for_file_name=parameter_value


        elif parameter == 'kpts_density':
            kpts=kdens2mp(self.atoms_fix,kptdensity=parameter_value['density']) #what happen if it is a slab?
            new_kdens=parameter_value['density']
            new_kpts=kpts.copy()
            while np.mean(kpts) == np.mean(new_kpts):
                new_kdens+=0.2
                new_kpts=kdens2mp(self.atoms_fix,kptdensity=new_kdens)
            parameter_value['density']=np.round(new_kdens,decimals=1)
            parameter_for_file_name=parameter_value['density']
    
        elif parameter == 'occupation_width':
            width = parameter_value['width']
            width/=10
            parameter_value['width']=width
            parameter_for_file_name=parameter_value['width']

        return parameter_value, parameter_for_file_name
    
    def results_analysis(self,
                        parameter: str,
                        report_path: str,
                        ):
        """
        Analyze the results.

        Parameters
        ----------
        
        parameter (REQUIRED):
            Parameter to do convergence test.

        report_path (REQUIRED):
            Path to the result report.
        """

        self.parameter_converge_dict[parameter]=self.assemble_dictionary(parameter)
        iters=len(self.parameter_converge_dict[parameter]['parameter_converge_lst'])
        if iters>2:
            primary_energy_difference,secondary_energy_difference=self.convergence_update(parameter,self.parameter_converge_dict[parameter],report_path)
        else:
            primary_energy_difference,secondary_energy_difference=math.inf,math.inf
        return iters,primary_energy_difference,secondary_energy_difference

    def convergence_update(self,
                        parameter,
                        single_parameter_converge_dict,
                        report_path):
        """
        Update convergence progress.

        Parameters
        ----------

        parameter (REQUIRED):
            Parameter to converge on.

        single_parameter_converge_dict (REQUIRED):
            Dictionary of the parameter convergence progress.

        report_path (REQUIRED):
            Path to the result report.
        """

        if len(single_parameter_converge_dict['gpw_file_path_lst']) < 3:
            calculator_setting = restart(single_parameter_converge_dict['gpw_file_path_lst'][-1])[1]
            msg.restart_report(parameter,calculator_setting.parameters,report_path)
            primary_energy_difference = math.inf
            secondary_energy_difference = math.inf
        else:
            energy_per_atom_lst=[]
            for traj_file_path in single_parameter_converge_dict['traj_file_path_lst']:
                atoms=read(traj_file_path)
                energy_per_atom_lst.append(atoms.get_potential_energy()/len(atoms))
            energy_per_atom_array = np.array(energy_per_atom_lst[-3:])
            energy_per_atom_array_rep = np.array((energy_per_atom_lst[-3:]+energy_per_atom_lst[-3:])[1:4])
            energy_difference_array = np.round(np.abs(energy_per_atom_array-energy_per_atom_array_rep),decimals=4)
            secondary_energy_difference = energy_difference_array[1]
            primary_energy_difference = max(energy_difference_array[0],energy_difference_array[2])
            msg.convergence_update_report(parameter,single_parameter_converge_dict,report_path,energy_difference_array)
        return primary_energy_difference, secondary_energy_difference


    def gather_converge_progress(
                                self,
                                parameter: str,
                                gpw_file_path: str,
                                traj_file_path: str,
                                ):
        """
        Gather convergence progress and sort the list from coarse-to-fine (parameters, gpw_file_path, traj_file_path)

        Parameters
        ----------

        parameter(REQUIRED):
            Parameter to converge on.

        gpw_file_path(REQUIRED):

        traj_file_path(REQUIRED):

        """
        gpw_file_path_lst=glob(gpw_file_path)
        traj_file_path_lst=glob(traj_file_path)
        gpw_file_name_lst=[path.split('/')[-1] for path in gpw_file_path_lst]
        parameter_converge_lst=[parameter_value.split('-')[-1][:-11] for parameter_value in gpw_file_name_lst]

        if parameter in ['h','occupation_width']:
            parameter_converge_lst=[float(parameter_value) for parameter_value in parameter_converge_lst]
            coarse_to_fine_order=np.argsort(parameter_converge_lst)[::-1]
        elif parameter in ['kpts']: 
            parameter_converge_lst=[tuple(map(int, parameter_value[1:-1].split(', '))) for parameter_value in parameter_converge_lst]
            parameter_mean_converge_lst=[np.mean(parameter_value) for parameter_value in parameter_converge_lst]
            coarse_to_fine_order=np.argsort(parameter_mean_converge_lst)
        elif parameter in ['kpts_density']:
            parameter_converge_lst=[float(parameter_value) for parameter_value in parameter_converge_lst]
            coarse_to_fine_order=np.argsort(parameter_converge_lst)
        coarse_to_fine_parameter_converge_lst=[parameter_converge_lst[i] for i in coarse_to_fine_order]
        coarse_to_fine_gpw_file_path_lst=[gpw_file_path_lst[i] for i in coarse_to_fine_order]
        coarse_to_fine_traj_file_path_lst=[traj_file_path_lst[i] for i in coarse_to_fine_order]
        return coarse_to_fine_parameter_converge_lst, coarse_to_fine_gpw_file_path_lst, coarse_to_fine_traj_file_path_lst

def number_of_layers_converge():
    pass
