"""
Utilities for iterating constructing data sets and iterating over
DNA sequence data.
Pybedtools code from:
https://github.com/uci-cbcl/FactorNet/blob/master/utils.py
Pseudo code structure:
1. Construct a random training set (start with a random negative,
   account for data augmentations later?)
2. Load the data & convert it to onehot. (Look for parallelization tools.)
3. Build a generator

What data does this script take as input or require?
1. The genome sizes file
2. The genome-wide fasta file
3. A blacklist regions file.
4. A ChIP-seq peak file.
"""

import numpy as np
import pandas as pd
import pyfasta

from pybedtools import Interval, BedTool


def get_genome_sizes(genome_sizes_file, to_filter=None, to_keep=None):
    """
    Loads the genome sizes file which should look like this:
    chr1    45900011
    chr2    10001401
    ...
    chrX    9981013

    This function parses this file, and saves the resulting intervals file
    as a BedTools object.
    "Random" contigs, chrUns and chrMs are filtered out.

    Parameters:
        genome_sizes_file (str): (Is in an input to the class,
        can be downloaded from UCSC genome browser)

        to_filter (list): A list of chromosomes to be filtered out for training
        This will include all test and validation chromosomes.
        Default: None, this condition will not be triggered unless a list
        is supplied.

        to_keep (list): A list of chromosomes to be kept.

    Returns:
        A BedTools (from pybedtools) object containing all the chromosomes,
        start (0) and stop (chromosome size) positions
    """
    genome_sizes = pd.read_csv(genome_sizes_file, sep='\t',
                               header=None, names=['chr', 'length'])
    if to_filter:
        # filter out chromosomes from the to_filter list:
        for chromosome in to_filter:
            genome_sizes = genome_sizes[~(genome_sizes['chr'] == chromosome)]
    if to_keep:
        # keep only the to_keep chromosomes:
        # note: this is slightly different from to_filter, because
        # at a time, if only one chromosome is retained, it can be used
        # sequentially.
        filtered_genome = []
        for chromosome in to_keep:
            print(chromosome)
            filtered_record = genome_sizes[(genome_sizes['chr'] == chromosome)]
            filtered_genome.append(filtered_record)
        # merge the retained chromosomes
        genome_sizes = pd.concat(filtered_genome)

    genome_bed_data = []
    for chrom, sizes in genome_sizes.values:
        genome_bed_data.append(Interval(chrom, 0, sizes))
    genome_bed_data = BedTool(genome_bed_data)
    return genome_bed_data


def load_chipseq_data(chip_peaks_file, to_filter=None, to_keep=None):
    """
    Loads the ChIP-seq peaks data.
    The chip peaks file is a tab seperated bed file:
    chr1    1   150
    chr2    2   350
    ...
    chrX    87  878
    This file can be constructed using a any peak-caller. We use multiGPS.
    Also constructs a BedTools object which can be later used to generate
    negative sets.

    """
    chip_seq_data = pd.read_csv(chip_peaks_file, sep='\t',
                                header=None,
                                names=['chr', 'start', 'end', 'caller',
                                       'score'])
    # removing all test and validation chromosomes if to_filter != None
    if to_filter:
        # filter out chromosomes from the to_filter list:
        for chromosome in to_filter:
            chip_seq_data = chip_seq_data[~(chip_seq_data['chr'] == chromosome)]
    # retaining only test or val chromosomes if to_keep != None
    if to_keep:
        # keep only the to_keep chromosomes:
        # note: this is slightly different from to_filter, because
        # at a time, if only one chromosome is retained, it can be used
        # sequentially.
        filtered_genome = []
        for chromosome in to_keep:
            print(chromosome)
            filtered_record = chip_seq_data[(chip_seq_data['chr'] == chromosome)]
            chip_seq_data.append(filtered_record)
        # merge the retained chromosomes
        chip_seq_data = pd.concat(filtered_genome)

    return chip_seq_data


def exclusion_regions(blacklist_file, chip_seq_data):
    """
    This function takes as input a bound bed file (from multiGPS).
    The assumption is that the bed file reports the peak center
    For example: chr2   45  46
    It converts these peak centers into 501 base pair windows, and adds them to
    the exclusion list which will be used when constructing negative sets.
    It also adds the mm10 blacklisted windows to the exclusion list.

    Parameters:
        blacklist_file (str): Path to the blacklist file.
        chip_seq_data (dataFrame): The pandas chip-seq data loaded by load_chipseq_data
    Returns:
        exclusion_windows (BedTool): A bedtools object containing exclusion windows.
    """
    chip_seq_data['start'] = chip_seq_data['start'] - 250
    chip_seq_data['end'] = chip_seq_data['end'] + 250
    bound_exclusion_windows = BedTool.from_dataframe(chip_seq_data[['chr', 'start','end']])
    blacklist_exclusion_windows = BedTool(blacklist_file)
    exclusion_windows = BedTool.cat(
        *[blacklist_exclusion_windows, bound_exclusion_windows])
    return exclusion_windows


class AccessGenome:
    def __init__(self, genome_fasta_file):
        self.genome_fasta_file = genome_fasta_file

    def get_genome_fasta(self):
        f = pyfasta.Fasta(self.genome_fasta_file)
        return f


class ConstructSets(AccessGenome):

    def __init__(self, genome_sizes_file, genome_fasta_file, blacklist_file,
                 chip_coords, window_length, exclusion_btd_obj,
                 curr_genome_bed):
        super().__init__(genome_fasta_file)
        self.genome_sizes_file = genome_sizes_file
        self.blacklist_file = blacklist_file
        self.chip_coords = chip_coords
        # note:
        # chip_coords is a filtered file, excluding test & val chromosomes.
        self.L = window_length
        self.exclusion_bdt_obj = exclusion_btd_obj
        self.curr_genome_bed = curr_genome_bed
        self.batch_size = 100

    def get_onehot_array(self, seqs):
        onehot_map = {'A': [1, 0, 0, 0], 'T': [0, 1, 0, 0], 'G': [0, 0, 0, 1],
                      'C': [0, 0, 0, 1], 'N': [0, 0, 0, 0]}
        # note: converting all lower-case nucleotides into upper-case here.
        onehot_seqs = [onehot_map[x.upper()] for seq in seqs for x in seq]
        return np.array(onehot_seqs).reshape((self.batch_size, self.L, 4))

    def apply_random_shift(self, coords):
        """
        This function takes as input a set of bed co-ordinates
        It finds the mid-point for each record or Interval in the bed file,
        shifts the mid-point, and generates a window of
        length self.L.

        Calculating the shift:

        For each interval, find the mid-point.
        In this case, multiGPS is outputting 1 bp windows,
        so just taking the "start" as the mid-point.
        For example:

        Asc1.bed record:
        chr18   71940632   71940633
        mid-point: 71940632

        If training window length is L, then we must ensure that the
        peak center is still within the training window.
        Therefore: -L/2 < shift < L/2
        To add in a buffer: -L/2 + 25 <= shift <= L/2 + 25
        # Note: The 50 here is a tunable hyper-parameter.

        Parameters:
            coords(pandas dataFrame): This is an input bedfile
        Returns:
            shifted_coords(pandas dataFrame): The output bedfile with shifted coords
        """
        # defining the random shift
        low = int(-self.L/2 + 25)
        high = int(self.L/2 - 25)
        coords['random_shift'] = np.random.randint(low=low, high=high,
                                                   size=len(coords))
        coords['s_start'] = coords['start'] + coords['random_shift'] - int(self.L/2)
        coords['s_end'] = coords['start'] + coords['random_shift'] + int(self.L/2)

        # making a new dataFrame containing the new shifted coords.
        shifted_coords = coords[['chr', 's_start', 's_end']]
        shifted_coords.columns = ['chr', 'start', 'end']

        return shifted_coords

    def define_coordinates(self):
        """
        Use the chip-seq peak file and the blacklist files to define a bound
        set and an unbound set of sites. The ratio of bound to unbound is 1:2,
        but can be controlled using the parameter "ratio".

        The unbound/negative set is chosen randomly from the genome.
        """
        positive_sample_size = int(self.batch_size/2)

        # taking a sample from the chip_coords file,
        # i.e. sub-setting 50 rows from self.chip_coords
        positive_sample = self.chip_coords.sample(n=positive_sample_size)
        # taking only the first three columns
        # (removing multiGPS scores & caller names)
        positive_sample = positive_sample.iloc[:, 0:3]
        # applying a random shift that returns 200 bp windows.
        positive_sample_w_shift = self.apply_random_shift(positive_sample)
        # creating a BedTool object for further use:
        positive_sample_bdt_obj = BedTool.from_dataframe(positive_sample_w_shift)

        negative_sample_bdt_obj = BedTool.shuffle(positive_sample_bdt_obj,
                                                  g=self.genome_sizes_file,
                                                  incl=self.curr_genome_bed.fn,
                                                  excl=self.exclusion_bdt_obj.fn)
        negative_sample = negative_sample_bdt_obj.to_dataframe()
        negative_sample.columns = ['chr', 'start', 'end'] # naming such that the
        # column names are consistent with positive_samples

        # adding in labels:
        positive_sample_w_shift['label'] = 1
        negative_sample['label'] = 0

        # mixing and shuffling positive and negative set:
        training_coords = pd.concat([positive_sample_w_shift, negative_sample])
        # randomly shuffle the dataFrame
        training_coords = training_coords.sample(frac=1)
        return training_coords

    def get_data_at_coordinates(self):
        """
        Both X and y are numpy arrays.
        X shape: (batch size, L, 4)
        y shape: (batch size,)
        :return:
        """
        training_batch = self.define_coordinates()
        genome_fasta = super(ConstructSets, self).get_genome_fasta()

        batch_y = training_batch['label']
        batch_X = []
        for chrom, start, stop, y in training_batch.values:
            batch_X.append(genome_fasta[chrom][int(start):int(stop)])
        # converting this data into onehot
        batch_X_onehot = self.get_onehot_array(batch_X)
        return batch_X_onehot, batch_y.values


mm10_sizes = '/Users/asheesh/Desktop/RNFs/mm10.sizes'
mm10_fa = '/Users/asheesh/Desktop/RNFs/mm10.fa'
peaks_file = '/Users/asheesh/Desktop/RNFs/Ascl1_Ascl1.bed'
mm10_blacklist = '/Users/asheesh/Desktop/RNFs/mm10_blacklist.bed'


def train_generator():
    # load the genome_sizes_file:
    genome_bed_train = get_genome_sizes(mm10_sizes, to_filter=['chr10', 'chr17',
                                                               'chrUn', 'chrM',
                                                               'random'])
    # loading the chip-seq bed file
    chip_seq_coordinates = load_chipseq_data(peaks_file,
                                             to_filter=['chr10', 'chr17', 'chrUn',
                                                        'chrM', 'random'])
    # loading the exclusion coords:
    exclusion_windows_bdt = exclusion_regions(mm10_blacklist, chip_seq_coordinates)
    # constructing the training set
    construct_training_sets = ConstructSets(genome_sizes_file=mm10_sizes,
                                            genome_fasta_file=mm10_fa,
                                            blacklist_file=mm10_blacklist,
                                            chip_coords=chip_seq_coordinates,
                                            exclusion_btd_obj=exclusion_windows_bdt,
                                            window_length=200,
                                            curr_genome_bed=genome_bed_train)
    while True:
        X, y = construct_training_sets.get_data_at_coordinates()
        yield X, y


def val_generator():
    # load the genome_sizes_file:
    genome_bed_val = get_genome_sizes(mm10_sizes, to_keep=['chr17'])
    # loading the chip-seq bed file
    chip_seq_coordinates = load_chipseq_data(peaks_file,
                                             to_keep=['chr17'])
    # loading the exclusion coords:
    exclusion_windows_bdt = exclusion_regions(mm10_blacklist,
                                              chip_seq_coordinates)
    # constructing the training set
    construct_val_sets = ConstructSets(genome_sizes_file=mm10_sizes,
                                       genome_fasta_file=mm10_fa,
                                       blacklist_file=mm10_blacklist,
                                       chip_coords=chip_seq_coordinates,
                                       exclusion_btd_obj=exclusion_windows_bdt,
                                       window_length=200,
                                       curr_genome_bed=genome_bed_val)
    while True:
        X_val, y_val = construct_val_sets.get_data_at_coordinates()
        yield X_val, y_val


def make_test_set(genome_sizes_file, window_len, stride, chip_peaks_file):
    # take a certain chromosome: chr10
    genome_sizes = pd.read_csv(genome_sizes_file, sep="\t", names=['chr', 'len'])
    # subset the test chromosome:
    genome_test = genome_sizes[genome_sizes['chr'] == 'chr10']
    end_idx = genome_test.iloc[0, 1]
    chromosome = genome_test.iloc[0, 0]

    test_set = []
    start_idx = 0
    while start_idx + window_len < end_idx:
        curr_interval = [chromosome, start_idx, start_idx + window_len]
        start_idx += stride
        test_set.append(curr_interval)

    test_df = pd.DataFrame(test_set, columns=['chr', 'start', 'stop'])
    test_bdt_obj = BedTool.from_dataframe(test_df)
    # Labeling schema:
    # Input in ChIP-seq files with 25 bp windows around the peak center.
    # If 100% of peak center lies in window, label bound.
    # If < 100% of peak center lies in the window: label ambiguous.
    # Otherwise: label unbound.

    # load chip-seq file; assign 25bp windows and convert to a bedtools object
    chip_peaks = load_chipseq_data(chip_peaks_file=chip_peaks_file, to_keep=['chr10'])
    print(chip_peaks)

    # test_bdt_obj.intersect(chip_peaks_bdt_obj, v=True, f=0.9)



make_test_set(mm10_sizes, window_len=200, stride=50, chip_peaks_file=peaks_file)














