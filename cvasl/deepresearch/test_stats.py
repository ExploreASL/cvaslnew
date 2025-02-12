import os
import pandas as pd
import numpy as np
import torch
from torch.utils.data import DataLoader
import matplotlib.pyplot as plt
import logging
import re
import argparse
import scipy.stats as stats
from scipy.stats import gaussian_kde
from scipy.stats import pearsonr
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from statsmodels.stats.diagnostic import het_breuschpagan, het_white, het_goldfeldquandt
import statsmodels.api as sm
import pingouin as pg
import scipy.stats as stats
import pandas as pd
import numpy as np
import torch
import logging
import os
from scipy.stats import zscore  # Import zscore from scipy.stats
from sklearn.metrics import mean_squared_error
from data import BrainAgeDataset
import json
from itertools import combinations
from scipy.stats import pearsonr, spearmanr  # Import correlation functions
# Set up logging
from models.cnn import Large3DCNN
from models.densenet3d import DenseNet3D
from models.efficientnet3d import EfficientNet3D
from models.improvedcnn3d import Improved3DCNN
from models.resnet3d import ResNet3D
from models.resnext3d import ResNeXt3D
import seaborn as sns
from scipy.stats import levene, bartlett, zscore
from utils import wrap_title
import pingouin as pg

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class BrainAgeAnalyzer:
    def __init__(self, train_csv, train_img_dir, val_csv, val_img_dir, model_dir, output_root, use_cuda=False, group_columns=["Sex", "Site", "Labelling"]): # Added group_columns as parameter
        self.train_csv = train_csv
        self.train_img_dir = train_img_dir
        self.val_csv = val_csv
        self.val_img_dir = val_img_dir
        self.model_dir = model_dir
        self.output_root = output_root
        self.group_cols = group_columns
        self.use_cuda = use_cuda
        os.makedirs(self.output_root, exist_ok=True)
        self.device = torch.device("cuda" if torch.cuda.is_available() and self.use_cuda else "cpu")
        logging.info(f"Using device: {self.device}")

        
        self.train_dataset = BrainAgeDataset(self.train_csv, self.train_img_dir)
        self.val_dataset = BrainAgeDataset(self.val_csv, self.val_img_dir)

    def load_model_from_name(self, model_path):
        """Loads a model based on its filename using load_model_with_params."""
        model_filename = os.path.basename(model_path)
        
        match = re.search(r'best__(.+?)_', model_filename)
        if match:
            model_type = match.group(1)
        else:
            model_type = 'unknown' 
        model = torch.load(model_path, map_location=self.device, weights_only = False)
        logging.info("Loaded model: %s of type %s", model_filename, model_type)
        return model, model_type
    
    def _create_summary_dataframe(self, results_bag, results_norm):
      """Helper function to create a summary DataFrame of test results."""
      df_bag = pd.DataFrame(results_bag).T.reset_index().rename(columns={'index': 'Test', 0: 'Statistic', 1: "P-value"})
      df_norm = pd.DataFrame(results_norm).T.reset_index().rename(columns={'index': 'Test', 0: 'Statistic', 1: "P-value"})
      df_bag['Data'] = 'BAG'
      df_norm['Data'] = 'Normative Deviation'

      df_summary = pd.concat([df_bag, df_norm], ignore_index=True)
      df_summary = df_summary[['Data', 'Test', 'Statistic', 'P-value']]
      return df_summary    

    def predict_ages(self, model, val_dataset):
        """Predicts ages for the validation dataset using the given model."""
        val_loader = DataLoader(val_dataset, batch_size=4, shuffle=False)
        all_predictions = []
        all_targets = []
        participant_ids = []
        all_demographics = [] 
        with torch.no_grad():
            for batch in val_loader:
                
                images = batch["image"].unsqueeze(1).to(self.device)
                ages = batch["age"].unsqueeze(1).to(self.device)
                demographics = batch["demographics"].cpu().numpy() 
                outputs = model(images, batch["demographics"].to(self.device))
                all_predictions.extend(outputs.cpu().numpy().flatten())
                all_targets.extend(ages.cpu().numpy().flatten())
                participant_ids.extend(batch["participant_id"])
                all_demographics.extend(demographics) 
        return participant_ids, all_predictions, all_targets, all_demographics 

    def analyze_heteroscedasticity(self, data, model_name, model_type, age_bins=None, alpha=0.05):
        """Analyzes heteroscedasticity of BAG across age bins using multiple tests and visualizations.

        Args:
            data (pd.DataFrame): DataFrame with 'actual_age', 'predicted_age', and 'brain_age_gap'.
            model_name (str): Name of the model.
            model_type (str): Type of the model.
            age_bins (np.ndarray, optional): Age bin edges. Defaults to np.linspace(20, 90, 8).
            alpha (float): Significance level for hypothesis tests. Defaults to 0.05.

        Returns:
            dict: Dictionary containing test results and binned data.
        """

        if data.empty:
            logging.warning(f"Input data is empty for {model_name}. Skipping analysis.")
            return {"results": None, "binned_data": None}

        if age_bins is None:
            age_bins = np.linspace(20, 90, 8)

        output_dir = os.path.join(self.output_root, "heteroscedasticity")
        
        
        os.makedirs(output_dir, exist_ok=True)
        binned_data = data.copy()

        
        binned_data['age_bin_str'] = pd.cut(binned_data['actual_age'], bins=age_bins,
                                            labels=[f"{age_bins[i]:.0f}-{age_bins[i + 1]:.0f}" for i in
                                                    range(len(age_bins) - 1)], include_lowest=True, right=True)
        binned_data['age_bin'] = pd.cut(binned_data['actual_age'], bins=age_bins, labels=False, include_lowest=True,
                                        right=True)

        
        results = {}

        
        bag_groups = [group['brain_age_gap'].values for _, group in binned_data.groupby('age_bin')]
        bag_groups = [group for group in bag_groups if len(group) > 0]
        if len(bag_groups) < 2:
            logging.warning(f"Skipping Levene's test for {model_name} due to insufficient data.")
            results['levene'] = {'statistic': np.nan, 'p_value': np.nan}
        else:
            levene_stat, levene_p_value = stats.levene(*bag_groups)
            results['levene'] = {'statistic': levene_stat, 'p_value': levene_p_value}

        
        try:
            bp_lm, bp_p_value, _, _ = het_breuschpagan(binned_data['brain_age_gap'],
                                                      sm.add_constant(binned_data['actual_age']))
            results['breusch_pagan'] = {'statistic': bp_lm, 'p_value': bp_p_value}
        except Exception as e:
            logging.warning(f"Breusch-Pagan test failed for {model_name}: {e}")
            results['breusch_pagan'] = {'statistic': np.nan, 'p_value': np.nan}

        
        try:
            white_lm, white_p_value, _, _ = het_white(binned_data['brain_age_gap'],
                                                     sm.add_constant(binned_data['actual_age']))
            results['white'] = {'statistic': white_lm, 'p_value': white_p_value}
        except Exception as e:
            logging.warning(f"White test failed for {model_name}: {e}")
            results['white'] = {'statistic': np.nan, 'p_value': np.nan}

        
        try:
            gq_f_value, gq_p_value, gq_order = het_goldfeldquandt(binned_data['brain_age_gap'],
                                                                   binned_data['actual_age'],
                                                                   alternative='increasing')
            results['goldfeld_quandt'] = {'statistic': gq_f_value, 'p_value': gq_p_value, 'order': gq_order}
        except Exception as e:
            logging.warning(f"Goldfeld-Quandt test failed for {model_name}: {e}")
            results['goldfeld_quandt'] = {'statistic': np.nan, 'p_value': np.nan, 'order': None}

        title = (
            "Brain Age Gap (BAG) vs. Actual Age. This plot shows the difference between predicted and actual age. "
            "Look for a 'funnel' shape: if the spread of points increases or decreases with age, it suggests heteroscedasticity."
        )
        plt.figure(figsize=(8, 6))
        plt.scatter(data['actual_age'], data['brain_age_gap'], alpha=0.5)
        plt.title(wrap_title(title), fontsize=9)
        plt.xlabel('Actual Age')
        plt.ylabel('Brain Age Gap (BAG)')
        plt.axhline(y=0, color='r', linestyle='--')
        plt.savefig(os.path.join(output_dir, f"bag_vs_actual_age.png"))
        plt.close()

        title = (
            "Residuals (BAG) vs. Fitted Values (Predicted Age). This plot shows how the errors (residuals) "
            "are distributed across the range of predicted ages. Look for non-uniform spread: if the spread changes "
            "systematically (e.g., wider at higher ages), it indicates heteroscedasticity."
        )
        plt.figure(figsize=(8, 6))
        plt.scatter(data['predicted_age'], data['brain_age_gap'], alpha=0.5)
        plt.title(wrap_title(title), fontsize=9)
        plt.xlabel('Predicted Age')
        plt.ylabel('Residuals (BAG)')
        plt.axhline(y=0, color='r', linestyle='--')
        plt.savefig(os.path.join(output_dir, f"residuals_vs_fitted.png"))
        plt.close()

        standardized_residuals = (data['brain_age_gap'] - data['brain_age_gap'].mean()) / data['brain_age_gap'].std()
        sqrt_abs_standardized_residuals = np.sqrt(np.abs(standardized_residuals))

        title = (
            "Scale-Location Plot. This plot shows the spread of residuals (square root of absolute standardized residuals) "
            "against predicted age. A horizontal red line indicates constant variance (homoscedasticity). "
            "An upward or downward sloping line suggests heteroscedasticity."
        )
        plt.figure(figsize=(8, 6))
        plt.scatter(data['predicted_age'], sqrt_abs_standardized_residuals, alpha=0.5)
        plt.title(wrap_title(title), fontsize=9)
        plt.xlabel('Predicted Age')
        plt.ylabel('√|Standardized Residuals|')
        sns.regplot(x=data['predicted_age'], y=sqrt_abs_standardized_residuals, scatter=False, lowess=True, line_kws={'color': 'red'})
        plt.savefig(os.path.join(output_dir, f"scale_location.png"))
        plt.close()

        title = (
            "Box Plots of BAG by Age Bin. Each box shows the distribution of BAG within an age group. "
            "Compare the box heights (interquartile range) and whisker lengths: if they differ substantially "
            "across bins, it suggests heteroscedasticity."
        )
        plt.figure(figsize=(10, 6))
        sns.boxplot(x='age_bin_str', y='brain_age_gap', data=binned_data)
        plt.title(wrap_title(title), fontsize=9)
        plt.xlabel('Age Bin')
        plt.ylabel('Brain Age Gap (BAG)')
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f"boxplots_bag.png"))
        plt.close()

        if 'levene' in results and not np.isnan(results['levene']['p_value']):
            title = (
                f"Levene's Test p-value. This bar shows the p-value from Levene's test for equal variances across age bins. "
                f"A p-value below the red line (significance level = {alpha}) suggests heteroscedasticity."
            )
            plt.figure(figsize=(6, 4))
            plt.bar(['Levene'], [results['levene']['p_value']], color=['skyblue'])
            plt.axhline(y=alpha, color='red', linestyle='--', label=f'Significance Level ({alpha})')
            plt.title(wrap_title(title), fontsize=9)
            plt.ylabel('p-value')
            plt.legend()
            plt.tight_layout()
            plt.savefig(os.path.join(output_dir, f"levene_pvalue_bar.png"))
            plt.close()
        summary = []
        summary.append(f"Heteroscedasticity Analysis for {model_name} ({model_type}):")
        for test_name, test_result in results.items():
            if not np.isnan(test_result['p_value']):
                significant = test_result['p_value'] < alpha
                summary.append(
                    f"  - {test_name.replace('_', ' ').title()}: Statistic = {test_result['statistic']:.3f}, "
                    f"p-value = {test_result['p_value']:.3f} "
                    f"({'Significant' if significant else 'Not Significant'} at alpha={alpha})"
                )
            else:
                summary.append(f"  - {test_name.replace('_', ' ').title()}: Test failed or not applicable.")

        overall_conclusion = "Overall: Evidence of heteroscedasticity." if any(
            results[test]['p_value'] < alpha for test in results if
            not np.isnan(results[test]['p_value'])) else "Overall: No significant evidence of heteroscedasticity."
        summary.append(overall_conclusion)
        summary.append("Implications: If heteroscedasticity is present, consider transforming the data (e.g., log transform) or using weighted least squares regression.")

        summary_str = "\n".join(summary)
        logging.info(summary_str)
        with open(os.path.join(output_dir, f"heteroscedasticity_summary.txt"), 'w') as f:
            f.write(summary_str)

        return {"results": results, "binned_data": binned_data}




    def calculate_normative_deviations(self, train_data, val_data, model_name, model_type, age_bins=np.linspace(20, 90, 8)):
        """Calculates normative deviations (z-scores of BAG) and assesses heteroscedasticity."""
        output_dir = os.path.join(self.output_root, "normative")
        os.makedirs(output_dir, exist_ok=True)

        train_binned_data = train_data.copy()
        val_binned_data = val_data.copy()

        train_binned_data['age_bin'] = pd.cut(train_binned_data['actual_age'], bins=age_bins, labels=False, include_lowest=True, right=True)
        val_binned_data['age_bin'] = pd.cut(val_binned_data['actual_age'], bins=age_bins, labels=False, include_lowest=True, right=True)

        normative_deviations = []
        heteroscedasticity_results = []  # Store test results

        for bin_label, bin_group in val_binned_data.groupby('age_bin'):
            if bin_label not in train_binned_data['age_bin'].unique():
                logging.warning(f"Age bin {bin_label} not found in training data, skipping.")
                continue

            train_bin_group = train_binned_data[train_binned_data['age_bin'] == bin_label]['brain_age_gap']
            val_bin_group = bin_group['brain_age_gap']

            if train_bin_group.empty:
                logging.warning(f"No training data for age bin {bin_label}, skipping.")
                continue

            mean_bag = train_bin_group.mean()
            std_bag = train_bin_group.std()

            if len(train_bin_group) > 2:  # Need at least 3 samples for these tests
                try:
                    levene_stat, levene_p = levene(train_bin_group, val_bin_group)
                    bartlett_stat, bartlett_p = bartlett(train_bin_group, val_bin_group)

                    heteroscedasticity_results.append({
                        'age_bin_label': f"{age_bins[int(bin_label)]:.1f}-{age_bins[int(bin_label)+1]:.1f}" if bin_label < len(age_bins)-1 else f">={age_bins[-1]:.1f}",
                        'levene_stat': levene_stat,
                        'levene_p': levene_p,
                        'bartlett_stat': bartlett_stat,
                        'bartlett_p': bartlett_p,
                    })
                except ValueError:
                    logging.warning(f"Heteroscedasticity tests failed for age bin {bin_label} (likely due to constant values).")
            else:
                logging.warning(f"Insufficient data for heteroscedasticity tests in age bin {bin_label}.")

            bin_norm_deviations = zscore(val_bin_group, ddof=1) if std_bag > 0 else np.zeros_like(val_bin_group)

            for i, bag_zscore in enumerate(bin_norm_deviations):
                normative_deviations.append({
                    'participant_id': bin_group.iloc[i]['participant_id'],
                    'normative_bag': bag_zscore,
                    'age_bin_label': f"{age_bins[int(bin_label)]:.1f}-{age_bins[int(bin_label)+1]:.1f}" if bin_label < len(age_bins)-1 else f">={age_bins[-1]:.1f}",
                })

        norm_dev_df = pd.DataFrame(normative_deviations)
        output_path_csv = os.path.join(output_dir, f"normative_deviations.csv")
        norm_dev_df.to_csv(output_path_csv, index=False)
        logging.info(f"Normative deviations saved to: {output_path_csv}")

        heteroscedasticity_df = pd.DataFrame(heteroscedasticity_results)
        if not heteroscedasticity_df.empty:
            output_path_het = os.path.join(output_dir, f"heteroscedasticity_tests.csv")
            heteroscedasticity_df.to_csv(output_path_het, index=False)
            logging.info(f"Heteroscedasticity test results saved to: {output_path_het}")


        self.plot_normative_deviations(train_binned_data, val_binned_data, model_name, model_type, age_bins, norm_dev_df)

        return norm_dev_df


    def plot_normative_deviations(self, train_data, val_data, model_name, model_type, age_bins, norm_dev_df):
        """Generates visualizations of the normative model and deviations."""
        output_dir = os.path.join(self.output_root, "normative")
        os.makedirs(output_dir, exist_ok=True)

        plt.figure(figsize=(10, 6))
        sns.scatterplot(x='actual_age', y='brain_age_gap', data=train_data, alpha=0.5, label='Training Data')
        
        bin_means = train_data.groupby('age_bin')['brain_age_gap'].mean()
        bin_stds = train_data.groupby('age_bin')['brain_age_gap'].std()
        bin_centers = (age_bins[:-1] + age_bins[1:]) / 2

        bin_means = bin_means.dropna()
        bin_stds = bin_stds.dropna()
        bin_centers_filtered = bin_centers[:len(bin_means)] # Make sure lengths match

        plt.plot(bin_centers_filtered, bin_means, color='red', label='Mean BAG')
        plt.fill_between(bin_centers_filtered, bin_means - bin_stds, bin_means + bin_stds, color='red', alpha=0.2, label='Mean ± 1 SD')
        
        plt.xlabel("Chronological Age")
        plt.ylabel("Brain Age Gap (BAG)")
        plt.title(wrap_title("Training Data: Brain Age Gap vs. Chronological Age.  Points show individual BAG values.  The red line shows the average BAG within each age bin, and the shaded area represents one standard deviation above and below this average.  This plot visualizes the expected BAG and its variability across age."),fontsize=9)
        plt.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f"bag_vs_age_train.png"))
        plt.close()


        plt.figure(figsize=(10, 6))
        # Merge normative deviations with validation data for plotting
        val_data_with_z = pd.merge(val_data, norm_dev_df, on='participant_id', how='left')

        cmap = plt.get_cmap('coolwarm')
        norm = plt.Normalize(val_data_with_z['normative_bag'].min(), val_data_with_z['normative_bag'].max())

        plt.scatter(val_data_with_z['actual_age'], val_data_with_z['brain_age_gap'], c=val_data_with_z['normative_bag'], cmap=cmap, norm=norm, alpha=0.7)
        plt.colorbar(label='Normative BAG (Z-score)')

        plt.xlabel("Chronological Age")
        plt.ylabel("Brain Age Gap (BAG)")
        plt.title(wrap_title("Validation Data: Brain Age Gap vs. Chronological Age with Z-scores. Each point represents an individual's BAG, colored by its normative deviation (z-score).  Red indicates higher BAG than expected for age, blue indicates lower BAG. The color intensity reflects the magnitude of the deviation."),fontsize=9)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f"bag_vs_age_val_zscore.png"))
        plt.close()



        plt.figure(figsize=(8, 5))
        sns.histplot(norm_dev_df['normative_bag'], kde=True)
        plt.xlabel("Normative BAG (Z-score)")
        plt.ylabel("Frequency")
        plt.title(wrap_title("Distribution of Normative Deviations (Z-scores). This histogram shows how the z-scores (normative BAG values) are distributed.  A standard normal distribution (mean=0, SD=1) is expected if the model assumptions hold. Deviations from this shape can indicate issues with the model or data."),fontsize=9)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f"normative_deviations_dist.png"))
        plt.close()


        plt.figure(figsize=(10, 6))
        sns.scatterplot(x='actual_age', y='normative_bag', data=val_data_with_z, alpha=0.7)
        plt.axhline(0, color='black', linestyle='--')  # Add a horizontal line at z=0
        plt.xlabel("Chronological Age")
        plt.ylabel("Normative BAG (Z-score)")
        plt.title(wrap_title("Validation Data: Normative Deviations (Z-scores) vs. Chronological Age. Each point represents an individual's z-score.  This plot helps visualize if there are any systematic trends in the deviations with age (e.g., increasing or decreasing variance), which could indicate model limitations."),fontsize=9)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f"normative_deviations_vs_age.png"))
        plt.close()


    def analyze_normative_deviation_stats(self, norm_dev_df, model_name, model_type):
        """Calculates descriptive statistics and tests for heteroscedasticity."""
        output_dir = os.path.join(self.output_root, "normative")
        os.makedirs(output_dir, exist_ok=True)

        norm_dev_stats = []
        all_normative_bags = []
        all_bin_labels = []

        for bin_label in sorted(norm_dev_df['age_bin_label'].unique()):
            bin_group = norm_dev_df[norm_dev_df['age_bin_label'] == bin_label]['normative_bag']

            bin_stat = {
                'age_bin_label': bin_label,
                'mean_norm_bag': bin_group.mean(),
                'std_norm_bag': bin_group.std(),
                'median_norm_bag': bin_group.median(),
                'iqr_norm_bag': np.percentile(bin_group, 75) - np.percentile(bin_group, 25),
                'count': bin_group.count()
            }
            norm_dev_stats.append(bin_stat)
            all_normative_bags.extend(bin_group.tolist())
            all_bin_labels.extend([bin_label] * bin_group.count()) # Add the bin labels for each value

        try:
            bf_stat, bf_pvalue = stats.levene(*[norm_dev_df[norm_dev_df['age_bin_label'] == lbl]['normative_bag'] for lbl in sorted(norm_dev_df['age_bin_label'].unique())], center='median')
            norm_dev_stats.append({
                'age_bin_label': 'Overall',
                'bf_statistic': bf_stat,
                'bf_pvalue': bf_pvalue
            })
        except ValueError as e:
            logging.warning(f"Brown-Forsythe test failed: {e}.  Likely due to a bin with only one sample.")
            norm_dev_stats.append({
                'age_bin_label': 'Overall',
                'bf_statistic': np.nan,
                'bf_pvalue': np.nan
            })

        norm_dev_stats_df = pd.DataFrame(norm_dev_stats)
        output_path_csv = os.path.join(output_dir, f"normative_deviation_stats.csv")
        norm_dev_stats_df.to_csv(output_path_csv, index=False)
        logging.info(f"Normative deviation statistics saved to: {output_path_csv}")

        self.visualize_normative_deviations(norm_dev_df, output_dir, model_name, model_type)

        return norm_dev_stats_df

    def visualize_normative_deviations(self, norm_dev_df, output_dir, model_name, model_type):
        """Generates visualizations of normative deviations."""

        plt.figure(figsize=(12, 6))
        sns.boxplot(x='age_bin_label', y='normative_bag', data=norm_dev_df, order=sorted(norm_dev_df['age_bin_label'].unique()))
        plt.axhline(0, color='red', linestyle='--')  # Add a horizontal line at z-BAG = 0
        plt.title(wrap_title("Distribution of Normalized Brain-Age Gap (z-BAG) Across Age Bins\nThis plot shows the spread of z-BAG values within each age group.  Each box represents an age bin, with the central line showing the median, the box edges showing the 25th and 75th percentiles (IQR), and the whiskers extending to the most extreme data points within 1.5 times the IQR.  Outliers beyond this range are plotted individually.  A z-BAG of 0 (red dashed line) indicates the predicted brain age matches the chronological age.  Values above 0 suggest an older-appearing brain, and values below 0 suggest a younger-appearing brain.  Compare the spread (box height and whisker length) across bins to assess heteroscedasticity:  wider boxes indicate greater variability in z-BAG for that age group."),fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Normalized Brain-Age Gap (z-BAG)")
        plt.xticks(rotation=45, ha="right")  # Rotate x-axis labels for readability
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f"boxplot_zBAG_by_age_bin.png"))
        plt.close()

        plt.figure(figsize=(12, 6))
        # Calculate mean and std within each bin, then merge back into the original dataframe
        bin_stats = norm_dev_df.groupby('age_bin_label')['normative_bag'].agg(['mean', 'std']).reset_index()
        norm_dev_df = norm_dev_df.merge(bin_stats, on='age_bin_label', suffixes=('', '_bin'))
        
        norm_dev_df['age_representative'] = norm_dev_df['age_bin_label'].apply(lambda x: np.mean([float(a) for a in x.split('-')]))

        plt.scatter(norm_dev_df['age_representative'], norm_dev_df['normative_bag'], alpha=0.5, label='Individual z-BAG')
        
        norm_dev_df = norm_dev_df.sort_values('age_representative')

        plt.plot(norm_dev_df['age_representative'], norm_dev_df['mean'], color='red', label='Mean z-BAG')

        plt.fill_between(norm_dev_df['age_representative'], norm_dev_df['mean'] - norm_dev_df['std'], norm_dev_df['mean'] + norm_dev_df['std'], color='red', alpha=0.2, label='Standard Deviation')


        plt.title(wrap_title("Scatterplot of Normalized Brain-Age Gap (z-BAG) vs. Age with Standard Deviation\nThis plot shows the relationship between age and z-BAG. Each point represents an individual.  The red line shows the average z-BAG within each age bin. The shaded red area represents one standard deviation above and below the mean z-BAG for each bin. Wider shaded areas indicate greater variability (heteroscedasticity) in the z-BAG for that age range.  The plot helps visualize how the spread of z-BAG changes with age."),fontsize=9)
        plt.xlabel("Age")
        plt.ylabel("Normalized Brain-Age Gap (z-BAG)")
        plt.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f"scatterplot_zBAG_vs_age.png"))
        plt.close()


        plt.figure(figsize=(12, 6))
        sns.violinplot(x='age_bin_label', y='normative_bag', data=norm_dev_df, order=sorted(norm_dev_df['age_bin_label'].unique()))
        plt.axhline(0, color='red', linestyle='--')
        plt.title(wrap_title("Distribution of Normalized Brain-Age Gap (z-BAG) Across Age Bins (Violin Plot)\nThis plot, similar to the boxplot, shows the distribution of z-BAG within each age group.  The wider parts of the violins indicate higher density of data points, showing the shape of the distribution. The white dot represents the median, the thick black bar the interquartile range (IQR), and the thin black line the 95% confidence interval. A z-BAG of 0 (red dashed line) indicates predicted brain age matches chronological age.  Compare the shape and spread of the violins to assess heteroscedasticity."),fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Normalized Brain-Age Gap (z-BAG)")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f"violinplot_zBAG_by_age_bin.png"))
        plt.close()
        
    def plot_normative_deviation_distribution(self, norm_dev_df, model_name, model_type):
        """Plots histogram and density plot for normative deviations (z-BAG)."""
        output_dir = self.output_root
        os.makedirs(output_dir, exist_ok=True)

        plt.figure(figsize=(8, 6))
        plt.hist(norm_dev_df["normative_bag"], bins=20, alpha=0.7, label="Normative BAG (Z-score)")
        try:
            kde = gaussian_kde(norm_dev_df["normative_bag"])
            x_grid = np.linspace(min(norm_dev_df["normative_bag"]), max(norm_dev_df["normative_bag"]), 1000)
            plt.plot(x_grid, kde(x_grid), color="blue", label="Density (Normative BAG)")
        except np.linalg.LinAlgError as e:
            logging.warning(f"Could not calculate density for normative BAG: {e}")
        plt.title(f"{model_name} - Distribution of Normative Brain Age Gap (Z-scores)")
        plt.xlabel("Normative Brain Age Gap (Z-score)")
        plt.ylabel("Frequency")
        plt.legend()
        plt.tight_layout()
        output_path = os.path.join(output_dir, f"normative_deviation_distribution.png")
        plt.savefig(output_path)
        plt.close()
        logging.info(f"Normative deviation distribution plot saved to: {output_path}")


    def calculate_ccc(self, data, model_name, model_type):
        """
        Calculates and visualizes the Concordance Correlation Coefficient (CCC)
        between predicted and actual age, along with other relevant metrics and plots.
        """
        output_dir = os.path.join(self.output_root, "ccc")
        os.makedirs(output_dir, exist_ok=True)

        if not isinstance(data, pd.DataFrame):
            try:
                data = pd.DataFrame(data)
            except:
                logging.error("Input 'data' must be a pandas DataFrame or convertible to one.")
                return np.nan
        if not {'actual_age', 'predicted_age'}.issubset(data.columns):
            logging.error("Input 'data' must contain 'actual_age' and 'predicted_age' columns.")
            return np.nan
        if not (pd.api.types.is_numeric_dtype(data['actual_age']) and pd.api.types.is_numeric_dtype(data['predicted_age'])):
            logging.error("'actual_age' and 'predicted_age' columns must contain numeric data.")
            return np.nan

        if data['actual_age'].isnull().any() or data['predicted_age'].isnull().any():
            logging.warning("NaN values found in 'actual_age' or 'predicted_age'. Removing rows with NaNs.")
            data = data.dropna(subset=['actual_age', 'predicted_age'])
        if len(data) == 0:
            logging.error("No valid data remaining after NaN removal.")
            return np.nan
        if data.empty:
            logging.error("'actual_age' and 'predicted_age' cannot be empty.")
            return np.nan


        long_data = pd.melt(data, value_vars=['actual_age', 'predicted_age'],
                            var_name='rater', value_name='age')
        long_data['subject'] = np.tile(np.arange(len(data)), 2) #repeats each index twice.
        icc_result = pg.intraclass_corr(data=long_data, targets='subject', raters='rater', ratings='age')

        ccc_value = icc_result[icc_result['Type'] == 'ICC3k']['ICC'].iloc[0]

        output_path_pingouin = os.path.join(output_dir, f"icc_details.csv")
        icc_result.to_csv(output_path_pingouin)
        logging.info(f"Detailed ICC results (from pingouin) saved to: {output_path_pingouin}")

        mae = np.mean(np.abs(data['predicted_age'] - data['actual_age']))
        rmse = np.sqrt(np.mean((data['predicted_age'] - data['actual_age'])**2))
        correlation = data['actual_age'].corr(data['predicted_age'])  # Pearson correlation

        output_path_txt = os.path.join(output_dir, f"metrics.txt")
        with open(output_path_txt, 'w') as f:
            f.write(f"CCC (Approximated by ICC3k): {ccc_value:.4f}\n")  # Clarify approximation
            f.write(f"Mean Absolute Error (MAE): {mae:.4f}\n")
            f.write(f"Root Mean Squared Error (RMSE): {rmse:.4f}\n")
            f.write(f"Pearson Correlation: {correlation:.4f}\n")
        logging.info(f"Metrics saved to: {output_path_txt}")
        plt.figure(figsize=(8, 6))
        plt.scatter(data['actual_age'], data['predicted_age'], alpha=0.5)
        plt.xlabel("Actual Age")
        plt.ylabel("Predicted Age")
        m, b = np.polyfit(data['actual_age'], data['predicted_age'], 1)  # Linear regression
        plt.plot(data['actual_age'], m * data['actual_age'] + b, color='red', label=f'y = {m:.2f}x + {b:.2f}')

        min_val = min(data['actual_age'].min(), data['predicted_age'].min())
        max_val = max(data['actual_age'].max(), data['predicted_age'].max())
        plt.plot([min_val, max_val], [min_val, max_val], color='black', linestyle='--', label='Perfect Agreement')

        plt.title(wrap_title(f"Scatter Plot of Predicted vs. Actual Age\nThis plot shows the relationship between predicted and actual ages. Each point represents a subject.  The red line is the best-fit line, and the dashed black line represents perfect agreement (where predicted age equals actual age). Deviations from the dashed line indicate prediction errors.  A tighter clustering around the dashed line suggests better model performance."),fontsize=9)
        plt.legend()
        plt.tight_layout()
        scatter_plot_path = os.path.join(output_dir, f"scatter_plot.png")
        plt.savefig(scatter_plot_path)
        plt.close()
        logging.info(f"Scatter plot saved to: {scatter_plot_path}")

        plt.figure(figsize=(8, 6))
        diff = data['predicted_age'] - data['actual_age']
        mean_age = (data['predicted_age'] + data['actual_age']) / 2
        plt.scatter(mean_age, diff, alpha=0.5)
        plt.axhline(np.mean(diff), color='red', linestyle='-', label=f'Mean Difference: {np.mean(diff):.2f}')
        plt.axhline(np.mean(diff) + 1.96*np.std(diff), color='red', linestyle='--', label=f'+1.96 SD: {np.mean(diff) + 1.96*np.std(diff):.2f}') #upper limit
        plt.axhline(np.mean(diff) - 1.96*np.std(diff), color='red', linestyle='--', label=f'-1.96 SD: {np.mean(diff) - 1.96*np.std(diff):.2f}') #lower limit
        plt.xlabel("Mean of Predicted and Actual Age")
        plt.ylabel("Difference (Predicted - Actual)")
        plt.title(wrap_title("Bland-Altman Plot of Age Prediction Differences\nThis plot shows the agreement between predicted and actual ages.  The x-axis represents the average of the predicted and actual ages, and the y-axis represents the difference between them. The solid red line shows the average difference (bias).  The dashed red lines represent the 95% limits of agreement (mean difference ± 1.96 times the standard deviation of the differences). Ideally, most points should fall within these limits, and the mean difference should be close to zero."),fontsize=9)
        plt.legend()
        plt.tight_layout()
        bland_altman_plot_path = os.path.join(output_dir, f"bland_altman_plot.png")
        plt.savefig(bland_altman_plot_path)
        plt.close()
        logging.info(f"Bland-Altman plot saved to: {bland_altman_plot_path}")

        plt.figure(figsize=(8, 6))
        residuals = data['predicted_age'] - data['actual_age']
        plt.hist(residuals, bins=20, edgecolor='black')
        plt.xlabel("Residuals (Predicted Age - Actual Age)")
        plt.ylabel("Frequency")
        plt.title(wrap_title("Histogram of Age Prediction Errors (Residuals)\nThis histogram shows the distribution of the prediction errors (residuals).  A well-performing model will have residuals centered around zero with a narrow, symmetrical distribution.  Skewness or a wide distribution indicates potential problems with the model's predictions."),fontsize=9)
        plt.tight_layout()
        histogram_path = os.path.join(output_dir, f"histogram.png")
        plt.savefig(histogram_path)
        plt.close()
        logging.info(f"Histogram of residuals saved to: {histogram_path}")

        return ccc_value

    def create_predictions_csv(self, participant_ids, predicted_ages, actual_ages, model_type, model_name):
        """Creates a CSV file with participant IDs, predicted ages, actual ages, and percentage error."""
        df = pd.DataFrame({
            "participant_id": participant_ids,
            "predicted_age": predicted_ages,
            "actual_age": actual_ages
        })
        df["brain_age_gap"] = df["predicted_age"] - df["actual_age"]
        df["percentage_error"] = (np.abs(df["brain_age_gap"]) / df["actual_age"]) * 100
        output_path = os.path.join(self.output_root, f"predictions.csv")
        df.to_csv(output_path, index=False)
        logging.info(f"Predictions saved to: {output_path}")

    def calculate_iqr(self, series):
        """Calculates the interquartile range (IQR) for a given series."""
        if len(series) <= 1:
            return np.nan
        return series.quantile(0.75) - series.quantile(0.25)

    def calculate_descriptive_stats(self, data, group_cols=None):
        """
        Calculates descriptive statistics for the given data.

        Args:
            data (pd.DataFrame): DataFrame containing 'predicted_age', 'actual_age',
                                 'brain_age_gap', and 'participant_id' columns.
            group_cols (list or str, optional): Column(s) to group by. Defaults to None.

        Returns:
            pd.DataFrame: DataFrame containing descriptive statistics.
        """

        for col in ["predicted_age", "actual_age", "brain_age_gap"]:
            if not pd.api.types.is_numeric_dtype(data[col]):
                raise ValueError(f"Column '{col}' must be numeric.")

        if group_cols:
            stats_df = data.groupby(group_cols).agg(
                mean_predicted_age=("predicted_age", "mean"),
                std_predicted_age=("predicted_age", "std"),
                median_predicted_age=("predicted_age", "median"),
                mean_actual_age=("actual_age", "mean"),
                std_actual_age=("actual_age", "std"),
                median_actual_age=("actual_age", "median"),
                mean_bag=("brain_age_gap", "mean"),
                std_bag=("brain_age_gap", "std"),
                median_bag=("brain_age_gap", "median"),
                count=("participant_id", "count"),
                iqr_predicted_age=("predicted_age", self.calculate_iqr),
                iqr_actual_age=("actual_age", self.calculate_iqr),
                iqr_bag=("brain_age_gap", self.calculate_iqr),
            )

        else:
            stats_df = data.agg(
                mean_predicted_age=("predicted_age", "mean"),
                std_predicted_age=("predicted_age", "std"),
                median_predicted_age=("predicted_age", "median"),
                mean_actual_age=("actual_age", "mean"),
                std_actual_age=("actual_age", "std"),
                median_actual_age=("actual_age", "median"),
                mean_bag=("brain_age_gap", "mean"),
                std_bag=("brain_age_gap", "std"),
                median_bag=("brain_age_gap", "median"),
                count=("participant_id", "count"),
            ).T

            iqr_values = {
                "iqr_predicted_age": self.calculate_iqr(data["predicted_age"]),
                "iqr_actual_age": self.calculate_iqr(data["actual_age"]),
                "iqr_bag": self.calculate_iqr(data["brain_age_gap"]),
            }
            iqr_df = pd.DataFrame([iqr_values])

            stats_df = pd.concat([stats_df, iqr_df], axis=1)
        self.visualize_descriptive_stats(data, stats_df, group_cols=group_cols)
        return stats_df
    
    
    def visualize_descriptive_stats(self, data, stats_df, group_cols=None, output_dir=None):
        """
        Generates and saves visualizations based on descriptive statistics.

        Args:
            data (pd.DataFrame):  The original data DataFrame.
            stats_df (pd.DataFrame): DataFrame of descriptive statistics from calculate_descriptive_stats.
            group_cols (list or str, optional):  Grouping columns (must match calculate_descriptive_stats).
            output_dir (str): Directory to save the plots.  Creates if it doesn't exist.
        """

        
        output_dir = os.path.join(self.output_root, "desc_stats") # Default output directory
        os.makedirs(output_dir, exist_ok=True)
        plt.figure(figsize=(8, 6))
        sns.regplot(x='actual_age', y='predicted_age', data=data, scatter_kws={'alpha':0.5}, line_kws={'color':'red'})
        min_val = min(data['actual_age'].min(), data['predicted_age'].min())
        max_val = max(data['actual_age'].max(), data['predicted_age'].max())
        plt.plot([min_val, max_val], [min_val, max_val], color='black', linestyle='--', label='Perfect Agreement')
        plt.title(wrap_title(f"Scatter Plot of Predicted vs. Actual Age\nThis plot shows the relationship between predicted and actual ages. Each point represents a subject.  The red line is the best-fit line, and the dashed black line represents perfect agreement (where predicted age equals actual age). Deviations from the dashed line indicate prediction errors.  A tighter clustering around the dashed line suggests better model performance."), fontsize=9)
        plt.xlabel("Actual Age")
        plt.ylabel("Predicted Age")
        plt.legend()
        plt.tight_layout()
        scatter_plot_path = os.path.join(output_dir, f"scatter_plot.png")
        plt.savefig(scatter_plot_path)
        plt.close()
        logging.info(f"Scatter plot saved to: {scatter_plot_path}")

        plt.figure(figsize=(8, 6))
        data['mean_age'] = (data['actual_age'] + data['predicted_age']) / 2
        sns.regplot(x='mean_age', y='brain_age_gap', data=data, scatter_kws={'alpha':0.5}, line_kws={'color':'red'})
        mean_diff = data['brain_age_gap'].mean()
        std_diff = data['brain_age_gap'].std()
        plt.axhline(mean_diff, color='blue', linestyle='-', label=f'Mean Difference: {mean_diff:.2f}')
        plt.axhline(mean_diff + 1.96 * std_diff, color='green', linestyle='--', label=f'+1.96 SD: {mean_diff + 1.96 * std_diff:.2f}')
        plt.axhline(mean_diff - 1.96 * std_diff, color='green', linestyle='--', label=f'-1.96 SD: {mean_diff - 1.96 * std_diff:.2f}')
        plt.title(wrap_title("Bland-Altman Plot\nThis plot assesses the agreement between predicted and actual ages.  The x-axis shows the average of predicted and actual age, and the y-axis shows the difference (predicted - actual). The blue line is the mean difference, and the green lines represent the 95% limits of agreement (mean ± 1.96 * standard deviation of the differences).  Points outside these limits are considered outliers."), fontsize=9)
        plt.xlabel("Mean of Predicted and Actual Age")
        plt.ylabel("Predicted Age - Actual Age (Brain Age Gap)")
        plt.legend()
        plt.tight_layout()
        bland_altman_plot_path = os.path.join(output_dir, f"bland_altman_plot.png")
        plt.savefig(bland_altman_plot_path)
        plt.close()
        logging.info(f"Bland-Altman plot saved to: {bland_altman_plot_path}")

        plt.figure(figsize=(8, 6))
        sns.histplot(data['brain_age_gap'], kde=True) 
        plt.title(wrap_title("Distribution of Brain Age Gap\nThis histogram shows the distribution of the brain age gap (predicted age - actual age).  A symmetrical distribution centered around zero indicates good model performance.  Skewness or a shift from zero suggests systematic over- or under-estimation."), fontsize=9)
        plt.xlabel("Brain Age Gap")
        plt.ylabel("Frequency")
        plt.tight_layout()
        bag_hist_path = os.path.join(output_dir, "brain_age_gap_histogram.png")
        plt.savefig(bag_hist_path)
        plt.close()
        logging.info(f"Brain Age Gap histogram saved to: {bag_hist_path}")

        if group_cols:
            if isinstance(group_cols, str):
                group_cols = [group_cols] 

            for group_col in group_cols:
                plt.figure(figsize=(10, 6))
                sns.boxplot(x=group_col, y='brain_age_gap', data=data)
                plt.title(wrap_title(f"Brain Age Gap Distribution by {group_col}\nThis box plot shows the distribution of the brain age gap for each group defined by '{group_col}'.  Comparing the medians, interquartile ranges, and presence of outliers across groups can reveal differences in model performance between groups."), fontsize=9)
                plt.xlabel(group_col)
                plt.ylabel("Brain Age Gap")
                plt.tight_layout()
                boxplot_path = os.path.join(output_dir, f"brain_age_gap_boxplot_{group_col}.png")
                plt.savefig(boxplot_path)
                plt.close()
                logging.info(f"Box plot for {group_col} saved to: {boxplot_path}")

                plt.figure(figsize=(12, 6))
                melted_data = pd.melt(data, id_vars=group_col, value_vars=['predicted_age', 'actual_age'], var_name='Type', value_name='Age')
                sns.boxplot(x=group_col, y='Age', hue='Type', data=melted_data)
                plt.title(wrap_title(f"Predicted and Actual Age Distribution by {group_col}\nThis box plot shows the distributions of both predicted and actual ages for each group in '{group_col}'.  Comparing the distributions helps assess whether the model performs differently across groups and whether there are systematic biases within specific groups."), fontsize=9)
                plt.xlabel(group_col)
                plt.ylabel("Age")
                plt.tight_layout()
                pred_vs_actual_boxplot_path = os.path.join(output_dir, f"pred_vs_actual_boxplot_{group_col}.png")
                plt.savefig(pred_vs_actual_boxplot_path)
                plt.close()
                logging.info(f"Predicted vs Actual box plot for {group_col} saved to: {pred_vs_actual_boxplot_path}")



        fig, axes = plt.subplots(1, 3, figsize=(18, 6))  # 1 row, 3 columns

        sns.histplot(data['predicted_age'], kde=True, ax=axes[0])
        axes[0].set_title(wrap_title("Distribution of Predicted Ages\nThis histogram displays the distribution of predicted ages across the dataset. It helps visualize the range and central tendency of the model's predictions."), fontsize=9)
        axes[0].set_xlabel("Predicted Age")
        axes[0].set_ylabel("Frequency")

        sns.histplot(data['actual_age'], kde=True, ax=axes[1])
        axes[1].set_title(wrap_title("Distribution of Actual Ages\nThis histogram shows the distribution of actual ages in the dataset. Comparing this to the predicted age distribution helps identify potential biases or discrepancies in the model's predictions relative to the true age distribution."), fontsize=9)
        axes[1].set_xlabel("Actual Age")
        axes[1].set_ylabel("Frequency")

        sns.histplot(data['brain_age_gap'], kde=True, ax=axes[2])
        axes[2].set_title(wrap_title("Distribution of Brain Age Gap\nThis histogram shows the distribution of the brain age gap (predicted age - actual age).  A symmetrical distribution centered around zero indicates good model performance.  Skewness or a shift from zero suggests systematic over- or under-estimation."), fontsize=9)
        axes[2].set_xlabel("Brain Age Gap")
        axes[2].set_ylabel("Frequency")

        plt.tight_layout()
        distributions_path = os.path.join(output_dir, "distributions.png")
        plt.savefig(distributions_path)
        plt.close()
        logging.info(f"Distribution plots saved to: {distributions_path}")



        plt.figure(figsize=(8, 6))
        correlation_matrix = data[['predicted_age', 'actual_age', 'brain_age_gap']].corr()
        sns.heatmap(correlation_matrix, annot=True, cmap="coolwarm", vmin=-1, vmax=1)
        plt.title(wrap_title("Correlation Matrix\nThis heatmap shows the Pearson correlation coefficients between predicted age, actual age, and brain age gap.  Strong positive correlations between predicted and actual age are desirable.  Correlations with brain age gap can indicate potential biases."), fontsize=9)
        plt.tight_layout()
        heatmap_path = os.path.join(output_dir, "correlation_heatmap.png")
        plt.savefig(heatmap_path)
        plt.close()
        logging.info(f"Correlation heatmap saved to: {heatmap_path}")    


    def analyze_bag_by_age_bins(self, data, model_name, model_type, age_bins=np.linspace(20, 90, 8)):
        """Analyzes Brain Age Gap (BAG) within different age bins.

        Args:
            data (pd.DataFrame): DataFrame containing 'actual_age', 'predicted_age', and 'brain_age_gap' columns.
            model_name (str): Name of the model.  (Used for filenames, etc. - not directly in this function)
            model_type (str): Type of the model. (Used for filenames, etc. - not directly in this function)
            age_bins (array-like):  The age bins to use. Bins are right-inclusive (e.g., age 30 falls into the 30-40 bin).

        Returns:
            tuple: (bin_stats_df, binned_data)
                bin_stats_df (pd.DataFrame): DataFrame containing BAG statistics for each age bin.
                binned_data (pd.DataFrame):  The input 'data' DataFrame with an added 'age_bin' column.
        """
        output_dir = os.path.join(self.output_root, "bag_age_bins")
        os.makedirs(output_dir, exist_ok=True)

        binned_data = data.copy()
        binned_data['age_bin'] = pd.cut(binned_data['actual_age'], bins=age_bins, labels=False, include_lowest=True, right=True)

        bin_stats = []
        for bin_label, bin_group in binned_data.groupby('age_bin'):
            bag_values = bin_group['brain_age_gap']
            actual_ages_bin = bin_group['actual_age']
            predicted_ages_bin = bin_group['predicted_age'] 

            epsilon = 1e-6 

            mape_bag = np.mean(np.abs(bag_values) / (actual_ages_bin + epsilon)) * 100 if not actual_ages_bin.empty else np.nan
            mdape_bag = np.median(np.abs(bag_values) / (actual_ages_bin + epsilon)) * 100 if not actual_ages_bin.empty else np.nan
            rmse_bag = np.sqrt(mean_squared_error([0] * len(bag_values), bag_values))

            bin_stat = {
                'age_bin_label': f"{age_bins[int(bin_label)]:.1f}-{age_bins[int(bin_label)+1]:.1f}" if bin_label < len(age_bins)-1 else f">={age_bins[int(bin_label)]:.1f}",
                'mean_bag': bag_values.mean(),
                'std_bag': bag_values.std(),
                'variance_bag': bag_values.var(),
                'median_bag': bag_values.median(),
                'iqr_bag': np.percentile(bag_values, 75) - np.percentile(bag_values, 25),
                'mape_bag': mape_bag,
                'mdape_bag': mdape_bag,
                'rmse_bag': rmse_bag,
                'count': bag_values.count()
            }
            bin_stats.append(bin_stat)

        bin_stats_df = pd.DataFrame(bin_stats)
        output_path = os.path.join(output_dir, f"bag_by_age_bin_stats.csv")
        bin_stats_df.to_csv(output_path, index=False)
        logging.info(f"BAG statistics by age bin saved to: {output_path}")
        self.visualize_bag_analysis(bin_stats_df, binned_data, model_name)
        return bin_stats_df, binned_data



    def visualize_bag_analysis(self, bin_stats_df, binned_data, model_name):
        """
        Visualizes the results of the BAG analysis by age bin.

        Args:
            bin_stats_df (pd.DataFrame): Output DataFrame from analyze_bag_by_age_bins (bin statistics).
            binned_data (pd.DataFrame): Output DataFrame from analyze_bag_by_age_bins (data with age bins).
            model_name (str):  The name of the model (used for file naming -  not directly in the plots).
        """

        output_dir = os.path.join(self.output_root, "bag_age_bins")
        os.makedirs(output_dir, exist_ok=True)
        plt.rcParams.update({'font.size': 9})

        plt.figure(figsize=(12, 6))
        plt.subplot(1, 2, 1)
        sns.boxplot(x='age_bin', y='brain_age_gap', data=binned_data, showmeans=True,
                    meanprops={"markerfacecolor": "red", "markeredgecolor": "black"})
        plt.title(wrap_title("Boxplot of Brain Age Gap (BAG) by Age Bin\nShows the distribution of BAG within each age group.  The box represents the interquartile range (IQR), the line is the median, and whiskers extend to 1.5*IQR.  Outliers are shown as individual points. The red triangle represents mean."),fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Brain Age Gap (Years)")

        plt.subplot(1, 2, 2)
        sns.violinplot(x='age_bin', y='brain_age_gap', data=binned_data, inner="quartile")
        plt.title(wrap_title("Violin Plot of Brain Age Gap (BAG) by Age Bin\nDisplays the distribution of BAG, showing the density of data points at different BAG values.  Wider sections indicate higher density. Lines represent the quartiles (25th, 50th, 75th percentiles) of the data within each bin."), fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Brain Age Gap (Years)")

        plt.tight_layout()
        bag_dist_path = os.path.join(output_dir, f"bag_distribution_by_age_bin.png")
        plt.savefig(bag_dist_path)
        plt.close()
        logging.info(f"BAG distribution plots saved to: {bag_dist_path}")


        plt.figure(figsize=(10, 6))
        bar_width = 0.35

        bin_stats_df['age_bin_label'] = bin_stats_df['age_bin_label'].astype(str)

        x = np.arange(len(bin_stats_df['age_bin_label']))

        plt.bar(x - bar_width/2, bin_stats_df['mean_bag'], bar_width, label='Mean BAG', color='skyblue')
        plt.bar(x + bar_width/2, bin_stats_df['median_bag'], bar_width, label='Median BAG', color='lightcoral')
        plt.xticks(x, bin_stats_df['age_bin_label'])
        plt.title(wrap_title("Mean and Median Brain Age Gap (BAG) by Age Bin\nCompares the mean (average) and median BAG for each age group.  Differences between the mean and median can highlight the presence of outliers or skewed distributions."), fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Brain Age Gap (Years)")
        plt.legend()
        plt.tight_layout()
        mean_median_bag_path = os.path.join(output_dir, f"mean_median_bag_by_age_bin.png")
        plt.savefig(mean_median_bag_path)
        plt.close()
        logging.info(f"Mean/Median BAG plot saved to: {mean_median_bag_path}")

        plt.figure(figsize=(10, 6))
        x = np.arange(len(bin_stats_df['age_bin_label']))

        plt.bar(x - bar_width, bin_stats_df['mape_bag'], bar_width, label='MAPE', color='mediumseagreen')
        plt.bar(x, bin_stats_df['mdape_bag'], bar_width, label='MdAPE', color='gold')
        plt.bar(x + bar_width, bin_stats_df['rmse_bag'], bar_width, label='RMSE', color='tomato')

        plt.xticks(x, bin_stats_df['age_bin_label'])
        plt.title(wrap_title("Error Metrics (MAPE, MdAPE, RMSE) by Age Bin\nShows different error measures for each age group. MAPE and MdAPE represent percentage errors, while RMSE is in the original unit (years).  Lower values indicate better performance."),fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Error Value")
        plt.legend()
        plt.tight_layout()
        error_metrics_path = os.path.join(output_dir, f"error_metrics_by_age_bin.png")
        plt.savefig(error_metrics_path)
        plt.close()
        logging.info(f"Error metrics plot saved to: {error_metrics_path}")

        plt.figure(figsize=(8, 5))
        plt.bar(bin_stats_df['age_bin_label'], bin_stats_df['count'], color='lightslategray')
        plt.title(wrap_title("Number of Subjects per Age Bin\nDisplays the number of subjects in each age group.  This helps assess the reliability of statistics in each bin; larger sample sizes generally lead to more reliable results."), fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Number of Subjects")
        plt.tight_layout()
        sample_size_path = os.path.join(output_dir, f"sample_size_by_age_bin.png")
        plt.savefig(sample_size_path)
        plt.close()
        logging.info(f"Sample size plot saved to: {sample_size_path}")

        plt.figure(figsize=(8, 6))
        sns.regplot(x='actual_age', y='brain_age_gap', data=binned_data, scatter_kws={'alpha':0.3}, line_kws={"color": "red"})
        plt.title(wrap_title("Brain Age Gap (BAG) vs. Actual Age\nShows the relationship between BAG and actual age.  Each point is a subject.  The red line is a trendline, indicating the general tendency of BAG across ages. Ideally, BAG should be centered around zero across all ages."), fontsize=9)
        plt.xlabel("Actual Age")
        plt.ylabel("Brain Age Gap (Years)")
        plt.axhline(0, color='black', linestyle='--')  # Add a horizontal line at BAG=0
        plt.tight_layout()
        bag_vs_age_path = os.path.join(output_dir, f"bag_vs_actual_age.png")
        plt.savefig(bag_vs_age_path)
        plt.close()
        logging.info(f"BAG vs. Actual Age plot saved to: {bag_vs_age_path}")

        num_bins = len(bin_stats_df['age_bin_label'])
        rows = int(np.ceil(np.sqrt(num_bins)))
        cols = int(np.ceil(num_bins / rows))
        plt.figure(figsize=(15, 15))

        for i, bin_label in enumerate(bin_stats_df['age_bin_label']):
          bin_data = binned_data[binned_data['age_bin'] == i]
          min_val = min(bin_data['actual_age'].min(), bin_data['predicted_age'].min())
          max_val = max(bin_data['actual_age'].max(), bin_data['predicted_age'].max())

          plt.subplot(rows, cols, i + 1)
          plt.scatter(bin_data['actual_age'], bin_data['predicted_age'], alpha=0.5)
          plt.plot([min_val, max_val], [min_val, max_val], color='black', linestyle='--', label='Perfect Agreement')
          plt.title(f"Bin: {bin_label}")
          plt.xlabel("Actual Age")
          plt.ylabel("Predicted Age")
          plt.legend()


        plt.suptitle(wrap_title("Predicted vs. Actual Age within Each Age Bin\nEach subplot shows the predicted vs. actual age for a specific age bin.  The dashed line represents perfect agreement.  This allows for a bin-specific assessment of prediction accuracy."), fontsize=12)
        plt.tight_layout(rect=[0, 0.03, 1, 0.95])
        pred_vs_actual_per_bin_path = os.path.join(output_dir, f"predicted_vs_actual_age_per_bin.png")
        plt.savefig(pred_vs_actual_per_bin_path)
        plt.close()
        logging.info(f"Predicted vs. Actual Age per Bin plot saved to: {pred_vs_actual_per_bin_path}")
        
    def calculate_icc(self, data):
        """Calculates Intra-Class Correlation Coefficient (ICC2) between predicted and actual age.

        This function calculates the ICC2 (two-way random effects, absolute agreement)
        to assess the agreement between the predicted age from the model and the
        actual chronological age.  ICC2 measures absolute agreement, meaning it
        penalizes systematic differences (bias) between predicted and actual ages.

        Args:
            data (pd.DataFrame): A DataFrame with columns 'participant_id', 'actual_age',
                                 and 'predicted_age'.

        Returns:
            tuple: A tuple containing:
                - icc_value (float): The calculated ICC2 value.
                - icc_results (pd.DataFrame): The full ICC results table from pingouin.intraclass_corr.

        Raises:
            ValueError: If the input DataFrame does not have the required columns.
        """
        output_dir = self.output_root
        os.makedirs(output_dir, exist_ok=True)

        if not all(col in data.columns for col in ['participant_id', 'actual_age', 'predicted_age']):
            raise ValueError("Input DataFrame must contain 'participant_id', 'actual_age', and 'predicted_age' columns.")

        icc_data = pd.melt(data,
                        id_vars=['participant_id'],
                        value_vars=['actual_age', 'predicted_age'],
                        var_name='Rater',
                        value_name='Age')

        icc_data = icc_data.rename(columns={'participant_id': 'Subject', 'Age': 'ratings'})

        icc_results = pg.intraclass_corr(data=icc_data, targets='Subject', raters='Rater', ratings='ratings')

        icc_value = icc_results.loc[icc_results['Type'] == 'ICC2', 'ICC'].values[0]

        output_path_csv = os.path.join(output_dir, f"icc_results.csv")
        icc_results.to_csv(output_path_csv)
        logging.info(f"ICC results saved to: {output_path_csv}")

        output_path_txt = os.path.join(output_dir, f"icc_value.txt")
        with open(output_path_txt, 'w') as f:
            f.write(f"ICC Value (ICC2): {icc_value:.4f}\n")
        logging.info(f"ICC value saved to: {output_path_txt}")
        self.visualize_and_analyze_icc(icc_value, icc_results, data)

        return icc_value, icc_results

    def visualize_and_analyze_icc(self, icc_value, icc_results, data):
        """Visualizes and analyzes Brain Age Gap (BAG) and ICC results.

        Performs a deep dive into the Brain Age Gap (BAG), including:
        - Scatter plot of predicted vs. actual age.
        - Bland-Altman plot of BAG vs. average age.
        - Histogram of BAG.
        - BAG analysis by age bins.
        - Calculates and reports Pearson and Spearman correlations.

        Args:
            icc_results_tuple (tuple): The output from calculate_icc, containing
                                       (icc_value, icc_results, data).
        """
        output_dir = os.path.join(self.output_root, "icc")
        os.makedirs(output_dir, exist_ok=True)

        data['BAG'] = data['predicted_age'] - data['actual_age']

        min_val = min(data['actual_age'].min(), data['predicted_age'].min())
        max_val = max(data['actual_age'].max(), data['predicted_age'].max())
        plt.figure(figsize=(8, 6))
        plt.scatter(data['actual_age'], data['predicted_age'], alpha=0.5)
        plt.plot([min_val, max_val], [min_val, max_val], color='black', linestyle='--', label='Perfect Agreement')
        sns.regplot(x='actual_age', y='predicted_age', data=data, scatter=False, color='red', label='Best-Fit Line')

        plt.title(wrap_title(f"Scatter Plot of Predicted vs. Actual Age\nThis plot visualizes the relationship between the ages predicted by the model and the actual ages of the subjects.  Each point represents a single subject. The black dashed line indicates perfect agreement (predicted age equals actual age).  The red line is the line of best fit.  Closer clustering around the dashed line indicates better model performance. Deviations represent prediction errors."),fontsize=9)

        plt.xlabel("Actual Age")
        plt.ylabel("Predicted Age")
        plt.legend()
        plt.tight_layout()
        scatter_plot_path = os.path.join(output_dir, f"scatter_plot.png")
        plt.savefig(scatter_plot_path)
        plt.close()
        logging.info(f"Scatter plot saved to: {scatter_plot_path}")

        data['average_age'] = (data['actual_age'] + data['predicted_age']) / 2
        mean_diff = data['BAG'].mean()
        std_diff = data['BAG'].std()

        plt.figure(figsize=(8, 6))
        plt.scatter(data['average_age'], data['BAG'], alpha=0.5)
        plt.axhline(mean_diff, color='blue', linestyle='-', label=f'Mean BAG: {mean_diff:.2f}')
        plt.axhline(mean_diff + 1.96 * std_diff, color='red', linestyle='--', label=f'+1.96 SD: {mean_diff + 1.96 * std_diff:.2f}')
        plt.axhline(mean_diff - 1.96 * std_diff, color='red', linestyle='--', label=f'-1.96 SD: {mean_diff - 1.96 * std_diff:.2f}')

        plt.title(wrap_title("Bland-Altman Plot of Brain Age Gap (BAG)\nThis plot assesses the agreement between predicted and actual ages by showing the difference (BAG) against the average age. The blue line represents the mean BAG, indicating any systematic bias.  The red dashed lines represent the 95% limits of agreement (mean ± 1.96 * standard deviation of BAG).  Ideally, most points should fall within these limits, and there should be no trend (e.g., increasing BAG with increasing age)."), fontsize=9)
        plt.xlabel("Average of Actual and Predicted Age")
        plt.ylabel("Brain Age Gap (BAG)")
        plt.legend()
        plt.tight_layout()
        bland_altman_path = os.path.join(output_dir, f"bland_altman_plot.png")
        plt.savefig(bland_altman_path)
        plt.close()
        logging.info(f"Bland-Altman plot saved to: {bland_altman_path}")

        plt.figure(figsize=(8, 6))
        sns.histplot(data['BAG'], kde=True)
        plt.title(wrap_title("Histogram of Brain Age Gap (BAG)\nThis histogram shows the distribution of BAG values.  A symmetrical distribution centered around zero indicates good model performance with no systematic over- or under-estimation.  The curve is a kernel density estimate, providing a smoothed representation of the distribution."), fontsize=9)

        plt.xlabel("Brain Age Gap (BAG)")
        plt.ylabel("Frequency")
        plt.tight_layout()
        histogram_path = os.path.join(output_dir, f"bag_histogram.png")
        plt.savefig(histogram_path)
        plt.close()
        logging.info(f"BAG histogram saved to: {histogram_path}")

        num_bins = 5  # Adjust as needed
        data['age_bin'] = pd.cut(data['actual_age'], bins=num_bins, labels=False)

        bag_stats = data.groupby('age_bin')['BAG'].agg(['mean', 'std', 'count']).reset_index()
        bag_stats['age_bin_label'] = data.groupby('age_bin')['actual_age'].apply(lambda x: f"{x.min():.1f}-{x.max():.1f}")
        bag_stats = bag_stats.rename(columns = {'mean':'BAG Mean', 'std':"BAG STD",	'count':"N"})
        print(bag_stats)
        plt.figure(figsize=(10, 6))
        plt.bar(bag_stats['age_bin_label'], bag_stats['BAG Mean'], yerr=bag_stats['BAG STD'], capsize=5)
        plt.title(wrap_title("Mean Brain Age Gap (BAG) by Age Bin\nThis plot shows the average BAG and its standard deviation for different age groups.  It helps to identify if the model's performance varies across different age ranges. Error bars represent the standard deviation of the BAG within each bin."), fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Mean BAG")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        bag_by_age_path = os.path.join(output_dir, f"bag_by_age_bin.png")
        plt.savefig(bag_by_age_path)
        plt.close()
        logging.info(f"BAG by age bin plot saved to: {bag_by_age_path}")
        bag_stats_path = os.path.join(output_dir, f"bag_stats_by_age.csv")
        bag_stats.to_csv(bag_stats_path)
        logging.info(f"BAG stats by age saved to: {bag_stats_path}")

        desc_stats = data[['actual_age', 'predicted_age', 'BAG']].describe()
        desc_stats_path = os.path.join(output_dir, "descriptive_statistics.csv")
        desc_stats.to_csv(desc_stats_path)
        logging.info(f"Descriptive statistics saved to: {desc_stats_path}")


        pearson_corr, pearson_p = pearsonr(data['actual_age'], data['predicted_age'])
        spearman_corr, spearman_p = spearmanr(data['actual_age'], data['predicted_age'])

        correlation_df = pd.DataFrame({
            'Correlation': ['Pearson', 'Spearman'],
            'Value': [pearson_corr, spearman_corr],
            'P-value': [pearson_p, spearman_p]
        })

        correlation_path = os.path.join(output_dir, "correlation_results.csv")
        correlation_df.to_csv(correlation_path, index=False)
        logging.info(f"Correlation results saved to: {correlation_path}")

        all_stats = {
            "Descriptive Statistics": desc_stats,
            "ICC Results": icc_results,
            "BAG Stats by Age": bag_stats,
            "Correlation Results": correlation_df
        }
        all_stats_path = os.path.join(output_dir, f"all_stats.txt")
        with open(all_stats_path, 'w') as f:
            for key, value in all_stats.items():
                f.write(f"\n--- {key} ---\n")
                f.write(str(value))
                f.write('\n')
        logging.info(f"all stats saved to: {all_stats_path}")

    def analyze_bias_variance_vs_age(self, data, model_name, model_type, age_bins=np.linspace(20, 90, 8)): # Added age_bins parameter
        """Analyzes bias and variance of predicted age across actual age distribution.

        Args:
            data (pd.DataFrame): DataFrame containing the following columns:
                - 'actual_age':  The true age of each subject.
                - 'predicted_age': The age predicted by the model.
                - 'brain_age_gap': The difference between predicted and actual age (predicted_age - actual_age).
            model_name (str): Name of the model (used for file naming, etc. - not directly used here).
            model_type (str): Type of the model (used for file naming, etc. - not directly used here).
            age_bins (np.ndarray):  Array defining the age bin edges.

        Returns:
            pd.DataFrame: A DataFrame where each row represents an age bin, and the columns
                contain bias, variance, and error metrics for that bin.  The columns are:
                - 'age_bin_label':  String representing the age range of the bin (e.g., "20.0-30.0").
                - 'bias':  The mean Brain Age Gap (BAG) within the bin.
                - 'variance_bag': The variance of the BAG within the bin.
                - 'variance_predicted_age': The variance of the predicted ages within the bin.
                - 'mape_predicted_age': Mean Absolute Percentage Error of predicted age within the bin.
                - 'mdape_predicted_age': Median Absolute Percentage Error of predicted age within the bin.
                - 'rmse_predicted_age': Root Mean Squared Error of predicted age within the bin.
                - 'count': The number of samples within the bin.
        """
        output_dir = os.path.join(self.output_root, "bias_variance_vs_age")
        os.makedirs(output_dir, exist_ok=True)

        binned_data = data.copy()
        binned_data['age_bin'] = pd.cut(binned_data['actual_age'], bins=age_bins, labels=False, include_lowest=True, right=True)


        bias_variance_stats = []
        for bin_label, bin_group in binned_data.groupby('age_bin'):
            bag_values_bin = bin_group['brain_age_gap']
            predicted_age_values = bin_group['predicted_age']
            actual_ages_bin = bin_group['actual_age'] 


            mape_predicted_age = np.mean(np.abs(predicted_age_values - actual_ages_bin) / actual_ages_bin) * 100 if not actual_ages_bin.empty else np.nan 
            mdape_predicted_age = np.median(np.abs(predicted_age_values - actual_ages_bin) / actual_ages_bin) * 100 if not actual_ages_bin.empty else np.nan 
            rmse_predicted_age = np.sqrt(mean_squared_error(actual_ages_bin, predicted_age_values)) 


            bin_stat = {
                'age_bin_label': f"{age_bins[int(bin_label)]:.1f}-{age_bins[int(bin_label)+1]:.1f}" if bin_label < len(age_bins)-1 else f">={age_bins[int(bin_label)]:.1f}", # Adjusted bin label
                'bias': bag_values_bin.mean(), 
                'variance_bag': bag_values_bin.var(), 
                'variance_predicted_age': predicted_age_values.var(), 
                'mape_predicted_age': mape_predicted_age, 
                'mdape_predicted_age': mdape_predicted_age, 
                'rmse_predicted_age': rmse_predicted_age, 
                'count': bag_values_bin.count()
            }
            bias_variance_stats.append(bin_stat)
        bias_variance_df = pd.DataFrame(bias_variance_stats)
        output_path = os.path.join(output_dir, f"bias_variance_vs_age_stats.csv")
        bias_variance_df.to_csv(output_path, index=False)
        logging.info(f"Bias and variance vs age statistics saved to: {output_path}")
        self.visualize_bias_variance(bias_variance_df, data)
        return bias_variance_df

    def visualize_bias_variance(self, bias_variance_df, data):
        """
        Visualizes the bias-variance analysis results with detailed plots.

        Args:
            bias_variance_df (pd.DataFrame): The output DataFrame from analyze_bias_variance_vs_age.
            data (pd.DataFrame): The original DataFrame used for the analysis, containing
                'actual_age', 'predicted_age', and 'brain_age_gap' columns.
        """

        output_dir = os.path.join(self.output_root, "bias_variance_vs_age")
        os.makedirs(output_dir, exist_ok=True)

        plt.figure(figsize=(10, 6))
        sns.barplot(x='age_bin_label', y='bias', data=bias_variance_df, color='skyblue')
        plt.title(wrap_title("Bias (Mean Brain Age Gap) vs. Age Bin\nThis plot shows the average difference between predicted and actual age (Brain Age Gap) for each age bin. Positive values indicate overestimation, and negative values indicate underestimation. Ideally, bias should be close to zero across all bins."),fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Bias (Mean BAG)")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        bias_plot_path = os.path.join(output_dir, "bias_vs_age_bin.png")
        plt.savefig(bias_plot_path)
        plt.close()
        logging.info(f"Bias plot saved to: {bias_plot_path}")

        plt.figure(figsize=(10, 6))
        sns.barplot(x='age_bin_label', y='variance_bag', data=bias_variance_df, color='lightcoral')
        plt.title(wrap_title("Variance of Brain Age Gap (BAG) vs. Age Bin\nThis plot shows the spread or variability of the Brain Age Gap (difference between predicted and actual age) within each age bin.  Higher variance indicates greater inconsistency in predictions within that age range."),fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Variance of BAG")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        variance_bag_plot_path = os.path.join(output_dir, "variance_bag_vs_age_bin.png")
        plt.savefig(variance_bag_plot_path)
        plt.close()
        logging.info(f"Variance (BAG) plot saved to: {variance_bag_plot_path}")


        plt.figure(figsize=(10, 6))
        sns.barplot(x='age_bin_label', y='variance_predicted_age', data=bias_variance_df, color='lightgreen')
        plt.title(wrap_title("Variance of Predicted Age vs. Age Bin\nThis plot displays the variability in predicted ages within each age bin.  While not directly measuring prediction error, it can highlight if the model's predictions are more spread out in certain age ranges, potentially indicating instability."),fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Variance of Predicted Age")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        variance_predicted_plot_path = os.path.join(output_dir, "variance_predicted_age_vs_age_bin.png")
        plt.savefig(variance_predicted_plot_path)
        plt.close()
        logging.info(f"Variance (Predicted Age) plot saved to: {variance_predicted_plot_path}")


        plt.figure(figsize=(10, 6))
        plt.plot(bias_variance_df['age_bin_label'], bias_variance_df['mape_predicted_age'], marker='o', label='MAPE')
        plt.plot(bias_variance_df['age_bin_label'], bias_variance_df['mdape_predicted_age'], marker='x', label='MdAPE')
        plt.plot(bias_variance_df['age_bin_label'], bias_variance_df['rmse_predicted_age'], marker='s', label='RMSE')
        plt.title(wrap_title("Error Metrics (MAPE, MdAPE, RMSE) vs. Age Bin\nThis plot shows three different error metrics across age bins. MAPE and MdAPE represent the average and median percentage error, respectively. RMSE is the Root Mean Squared Error. Lower values indicate better performance. Comparing these metrics can reveal the presence of outliers (larger difference between MAPE and MdAPE)."),fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Error Value")
        plt.xticks(rotation=45, ha="right")
        plt.legend()
        plt.tight_layout()
        error_metrics_plot_path = os.path.join(output_dir, "error_metrics_vs_age_bin.png")
        plt.savefig(error_metrics_plot_path)
        plt.close()
        logging.info(f"Error metrics plot saved to: {error_metrics_plot_path}")


        plt.figure(figsize=(10, 6))
        sns.barplot(x='age_bin_label', y='count', data=bias_variance_df, color='lightgray')
        plt.title(wrap_title("Number of Samples per Age Bin\nThis plot shows the number of data points within each age bin.  It's important to consider this when interpreting the other plots, as bins with very few samples may have less reliable statistics (bias, variance, etc.)."),fontsize=9)
        plt.xlabel("Age Bin")
        plt.ylabel("Count")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        count_plot_path = os.path.join(output_dir, "count_vs_age_bin.png")
        plt.savefig(count_plot_path)
        plt.close()
        logging.info(f"Count plot saved to: {count_plot_path}")


        plt.figure(figsize=(10, 6))
        sns.regplot(x='actual_age', y='predicted_age', data=data, scatter_kws={'alpha':0.3}, line_kws={'color':'red'})
        min_val = min(data['actual_age'].min(), data['predicted_age'].min())
        max_val = max(data['actual_age'].max(), data['predicted_age'].max())
        plt.plot([min_val, max_val], [min_val, max_val], color='black', linestyle='--', label='Perfect Agreement')

        plt.title(wrap_title(f"Scatter Plot of Predicted vs. Actual Age\nThis plot shows the relationship between predicted and actual ages. Each point represents a subject.  The red line is the best-fit line, and the dashed black line represents perfect agreement (where predicted age equals actual age). Deviations from the dashed line indicate prediction errors.  A tighter clustering around the dashed line suggests better model performance."),fontsize=9)
        plt.legend()
        plt.tight_layout()
        scatter_plot_path = os.path.join(output_dir, f"scatter_plot.png")
        plt.savefig(scatter_plot_path)
        plt.close()
        logging.info(f"Scatter plot saved to: {scatter_plot_path}")


        plt.figure(figsize=(10, 6))
        sns.histplot(data['brain_age_gap'], kde=True, color='purple')
        plt.title(wrap_title("Distribution of Brain Age Gap (BAG)\nThis histogram shows the distribution of the Brain Age Gap (predicted age - actual age).  A symmetrical distribution centered around zero suggests unbiased predictions.  The curve (Kernel Density Estimate) provides a smoothed representation of the distribution."),fontsize=9)
        plt.xlabel("Brain Age Gap")
        plt.ylabel("Frequency")
        plt.tight_layout()
        bag_distribution_plot_path = os.path.join(output_dir, "bag_distribution.png")
        plt.savefig(bag_distribution_plot_path)
        plt.close()
        logging.info(f"BAG distribution plot saved to: {bag_distribution_plot_path}")


        plt.figure(figsize=(10, 6))
        sns.scatterplot(x='actual_age', y='brain_age_gap', data=data, alpha=0.5, color='orange')
        plt.axhline(y=0, color='black', linestyle='--') # Add a horizontal line at y=0
        plt.title(wrap_title("Brain Age Gap (BAG) vs. Actual Age\nThis scatter plot shows the relationship between the Brain Age Gap (prediction error) and the actual age.  Ideally, the points should be randomly scattered around the horizontal dashed line at zero, indicating no systematic bias across the age range."),fontsize=9)
        plt.xlabel("Actual Age")
        plt.ylabel("Brain Age Gap")
        plt.tight_layout()
        bag_vs_age_plot_path = os.path.join(output_dir, "bag_vs_actual_age.png")
        plt.savefig(bag_vs_age_plot_path)
        plt.close()
        logging.info(f"BAG vs. Actual Age plot saved to: {bag_vs_age_plot_path}")
        
    def analyze_bag_demographic_correlation(self, data, model_name, model_type, demographic_cols=["Sex", "Site", "Labelling", "Readout"]):
        """
        Analyzes correlation between BAG and demographic variables, handling categorical data and multiple comparisons.
        """
        output_dir = self.output_root
        os.makedirs(output_dir, exist_ok=True)

        data = data.copy() 
        data.dropna(subset=['brain_age_gap'] + demographic_cols, inplace=True) 

        encoded_data = data.copy()
        for col in demographic_cols:
            if col in encoded_data.columns:
                if encoded_data[col].dtype == 'object': 
                    if len(encoded_data[col].unique()) == 2:
                        encoded_data[col] = encoded_data[col].astype('category').cat.codes
                    else:
                        encoded_data = pd.get_dummies(encoded_data, columns=[col], prefix=col, dummy_na=False) 
                elif encoded_data[col].dtype == 'int64' or encoded_data[col].dtype == 'float64':
                    pass
                else:
                    logging.warning("Data in unexpected format. Check it is numerical or object type")
                    return None

        correlation_results = []
        for demo_col in encoded_data.columns:
            if demo_col.startswith(tuple(demographic_cols)) and demo_col != 'brain_age_gap':  
                if len(encoded_data[demo_col].unique()) == 2:
                    correlation, p_value = pointbiserialr(encoded_data['brain_age_gap'], encoded_data[demo_col])
                    corr_type = 'pointbiserial'
                elif encoded_data[demo_col].dtype in ['int64', 'float64']:
                    correlation, p_value = spearmanr(encoded_data['brain_age_gap'], encoded_data[demo_col])
                    corr_type = 'spearman'
                else: 
                    logging.warning(f"Skipping correlation for {demo_col}: Unsupported data type.")
                    continue

                correlation_results.append({
                    'demographic_variable': demo_col,
                    'correlation': correlation,
                    'p_value': p_value,
                    'correlation_type': corr_type
                })

        if correlation_results:  
            correlation_df = pd.DataFrame(correlation_results)
            p_values = correlation_df['p_value'].values
            reject, pvals_corrected, _, _ = multipletests(p_values, alpha=0.05, method='fdr_bh') 
            correlation_df['p_value_corrected'] = pvals_corrected
            correlation_df['significant'] = reject
        else:
            correlation_df = pd.DataFrame()

        output_path = os.path.join(output_dir, f"bag_demographic_correlation.csv")
        correlation_df.to_csv(output_path, index=False)
        logging.info(f"BAG demographic correlation analysis saved to: {output_path}")
        self.visualize_bag_correlations(correlation_df, data, demographic_cols)
        return correlation_df

    def visualize_bag_correlations(self, correlation_df, data, demographic_cols=["Sex", "Site", "Labelling", "Readout"]):
        """
        Visualizes the correlations between BAG and demographics, including descriptive statistics.
        """
        if correlation_df.empty:
            logging.warning("Correlation DataFrame is empty. No visualizations generated.")
            return

        output_dir = os.path.join(self.output_root, "bag_demographic_correlation")
        os.makedirs(output_dir, exist_ok=True)
        data = data.copy()
        data.dropna(subset=['brain_age_gap'] + demographic_cols, inplace=True)
        
        desc_stats = data['brain_age_gap'].describe()
        desc_stats_df = pd.DataFrame(desc_stats)
        desc_stats_path = os.path.join(output_dir, "brain_age_gap_descriptive_statistics.csv")
        desc_stats_df.to_csv(desc_stats_path)
        logging.info(f"Descriptive statistics for brain_age_gap saved to: {desc_stats_path}")

        
        encoded_data = data.copy()
        for col in demographic_cols:
            if col in encoded_data.columns:
                if encoded_data[col].dtype == 'object':
                    if len(encoded_data[col].unique()) == 2:
                        
                        encoded_data[col] = encoded_data[col].astype('category').cat.codes
                    else:
                        
                        encoded_data = pd.get_dummies(encoded_data, columns=[col], prefix=col, dummy_na=False) 
                elif encoded_data[col].dtype == 'int64' or encoded_data[col].dtype == 'float64':
                    pass
                else:
                    logging.warning("Data in unexpected format. Check it is numerical or object type")
                    return None


        for _, row in correlation_df.iterrows():
            demo_col = row['demographic_variable']
            corr = row['correlation']
            p_val = row['p_value_corrected']
            corr_type = row['correlation_type']

            if corr_type == 'spearman':
                plt.figure(figsize=(8, 6))
                sns.regplot(x=demo_col, y='brain_age_gap', data=encoded_data,  line_kws={"color": "red"})

                title = (f"Scatter Plot of Brain Age Gap vs. {demo_col}\n"
                        f"Correlation: {corr:.3f}, Corrected p-value: {p_val:.3f}\n"
                        f"This plot visualizes the relationship between Brain Age Gap and {demo_col}. "
                        f"Each point is a subject. A positive correlation means higher values of {demo_col} "
                        f"tend to be associated with higher Brain Age Gap. The red line is the best fit."
                        f"A significant p-value (typically < 0.05) indicates a statistically significant association.")
                plt.title("\n".join(wrap(title, 60)), fontsize=9)
                plt.xlabel(demo_col)
                plt.ylabel("Brain Age Gap")
                plt.tight_layout()
                scatter_plot_path = os.path.join(output_dir, f"scatter_plot_{demo_col}.png")
                plt.savefig(scatter_plot_path)
                plt.close()
                logging.info(f"Scatter plot saved to: {scatter_plot_path}")

            elif corr_type == 'pointbiserial':
                original_col_name = demo_col.split("_")[0]
                plt.figure(figsize=(8, 6))
                sns.boxplot(x=original_col_name, y='brain_age_gap', data=data) 

                title = (f"Box Plot of Brain Age Gap by {original_col_name}\n"
                        f"Correlation: {corr:.3f}, Corrected p-value: {p_val:.3f}\n"
                        f"This box plot shows the distribution of Brain Age Gap for each category of {original_col_name}.  "
                        f"The box represents the interquartile range (IQR), the line inside the box is the median.  "
                        f"Whiskers extend to 1.5 times the IQR.  Points beyond the whiskers are potential outliers.  "
                        f"A significant p-value suggests the Brain Age Gap differs significantly between the groups.")
                plt.title("\n".join(wrap(title, 60)), fontsize=9)
                plt.xlabel(original_col_name)
                plt.ylabel("Brain Age Gap")
                plt.tight_layout()
                box_plot_path = os.path.join(output_dir, f"box_plot_{demo_col}.png")
                plt.savefig(box_plot_path)
                plt.close()
                logging.info(f"Box plot saved to: {box_plot_path}")

        
        plt.figure(figsize=(8, 6))
        sns.histplot(data['brain_age_gap'], kde=True)
        title = (f"Histogram of Brain Age Gap\n"
                    f"This histogram visualizes the distribution of Brain Age Gap values. "
                    f"The x-axis shows the Brain Age Gap, and the y-axis shows the frequency (count) of subjects within each bin. "
                    f"The curve is a Kernel Density Estimate (KDE), providing a smoothed representation of the distribution.")
        plt.title("\n".join(wrap(title, 60)), fontsize=9)

        plt.xlabel("Brain Age Gap")
        plt.ylabel("Frequency")
        plt.tight_layout()
        hist_path = os.path.join(output_dir, f"histogram_brain_age_gap.png")
        plt.savefig(hist_path)
        plt.close()
        logging.info(f"Histogram saved to: {hist_path}")
            
    def plot_metrics_vs_age(self, data, model_name, model_type, age_bins=np.linspace(20, 90, 8)): # Added age_bins parameter
        """Plots MAE of predicted age vs. actual age bins."""
        output_dir = os.path.join(self.output_root, "metrics_vs_age")
        os.makedirs(output_dir, exist_ok=True)

        binned_data = data.copy()
        binned_data['age_bin'] = pd.cut(binned_data['actual_age'], bins=age_bins, labels=False, include_lowest=True, right=True) # Right-inclusive bins


        metrics_vs_age_stats = []
        for bin_label, bin_group in binned_data.groupby('age_bin'):
            actual_ages_bin = bin_group['actual_age']
            predicted_ages_bin = bin_group['predicted_age']

            mae_bin = mean_absolute_error(actual_ages_bin, predicted_ages_bin) # MAE for this age bin
            rmse_bin = np.sqrt(mean_squared_error(actual_ages_bin, predicted_ages_bin)) # RMSE
            r2_bin = r2_score(actual_ages_bin, predicted_ages_bin) # R2
            pearson_bin, _ = pearsonr(actual_ages_bin, predicted_ages_bin) # Pearson

            bin_stat = {
                'age_bin_label': f"{age_bins[int(bin_label)]:.1f}-{age_bins[int(bin_label)+1]:.1f}" if bin_label < len(age_bins)-1 else f">={age_bins[int(bin_label)]:.1f}", # Adjusted bin label
                'mae': mae_bin, # MAE
                'rmse': rmse_bin, # RMSE
                'r2': r2_bin, # R2
                'pearson': pearson_bin, # Pearson
                'count': len(bin_group)
            }
            metrics_vs_age_stats.append(bin_stat)
        metrics_vs_age_df = pd.DataFrame(metrics_vs_age_stats)


        # Plotting MAE vs Age Bin
        plt.figure(figsize=(8, 6))
        plt.plot(metrics_vs_age_df['age_bin_label'], metrics_vs_age_df['mae'], marker='o', linestyle='-')
        plt.title(f'{model_name} - MAE vs. Age Bin')
        plt.xlabel('Age Bin (Years)')
        plt.ylabel('Mean Absolute Error (MAE)')
        plt.grid(True)
        plt.xticks(rotation=45, ha='right') # Rotate x-axis labels
        plt.tight_layout()
        output_path = os.path.join(output_dir, f"mae_vs_age_bin.png")
        plt.savefig(output_path)
        plt.close()
        logging.info(f"MAE vs Age Bin plot saved to: {output_path}")
        return metrics_vs_age_df

    @staticmethod
    def cohen_d(group1, group2):
        """Calculates Cohen's d for two groups."""
        diff = group1.mean() - group2.mean()
        n1, n2 = len(group1), len(group2)
        var1, var2 = group1.var(), group2.var()
        pooled_var = ((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2)
        return diff / np.sqrt(pooled_var + 1e-9) # Added epsilon to denominator


    def calculate_effect_sizes(self, data, group_cols, reference_group=None):
        """
        Calculates Cohen's d for predicted age and BAG between groups.

        Args:
            data: DataFrame with predicted_age, BAG, and group columns.
            group_cols: List of columns defining the groups.
            reference_group: Optional. The name of the reference group for comparisons.
                             If None, all pairwise comparisons are made.

        Returns:
            DataFrame with effect sizes.
        """

        results = []

        unique_groups = data.groupby(group_cols).groups.keys()

        if reference_group:
            comparisons = [
                (reference_group, group) for group in unique_groups if group != reference_group
            ]
        else:
            comparisons = list(combinations(unique_groups, 2))

        for group1_keys, group2_keys in comparisons:
            group1 = data.loc[
                data[group_cols].apply(
                    lambda row: tuple(row.values) == group1_keys, axis=1
                ),
                "predicted_age",
            ]
            group2 = data.loc[
                data[group_cols].apply(
                    lambda row: tuple(row.values) == group2_keys, axis=1
                ),
                "predicted_age",
            ]

            group1_bag = data.loc[
                data[group_cols].apply(
                    lambda row: tuple(row.values) == group1_keys, axis=1
                ),
                "brain_age_gap",
            ]  # Corrected column name to brain_age_gap
            group2_bag = data.loc[
                data[group_cols].apply(
                    lambda row: tuple(row.values) == group2_keys, axis=1
                ),
                "brain_age_gap",
            ]  # Corrected column name to brain_age_gap

            d_predicted_age = self.cohen_d(group1, group2)
            d_bag = self.cohen_d(group1_bag, group2_bag)

            results.append(
                {
                    "group_comparison": f"{group1_keys} vs {group2_keys}",
                    "cohen_d_predicted_age": d_predicted_age,
                    "cohen_d_bag": d_bag,
                }
            )
        results = pd.DataFrame(results)
        self.visualize_effect_sizes(results, data, group_cols)
        return results
    
    def visualize_effect_sizes(self, effect_sizes_df, data, group_cols):
        """
        Visualizes and analyzes the effect sizes calculated by calculate_effect_sizes.

        Args:
            effect_sizes_df: DataFrame returned by calculate_effect_sizes.
            data: The original DataFrame used for calculations.
            group_cols: The grouping columns.

        """
        output_dir = os.path.join(self.output_root, "effect_size")
        os.makedirs(output_dir, exist_ok=True)

        # 1. Cohen's d Bar Plots
        plt.figure(figsize=(12, 6))

        plt.subplot(1, 2, 1)
        sns.barplot(
            y="group_comparison", x="cohen_d_predicted_age", data=effect_sizes_df
        )
        plt.title(
            wrap_title(
                "Cohen's d for Predicted Age Between Groups\n"
                "This bar plot shows the standardized difference (Cohen's d) in predicted age between groups. "
                "Larger absolute values indicate larger effect sizes (larger differences between groups)."
            ),
            fontsize=9,
        )
        plt.xlabel("Cohen's d")
        plt.ylabel("Group Comparison")

        plt.subplot(1, 2, 2)
        sns.barplot(y="group_comparison", x="cohen_d_bag", data=effect_sizes_df)
        plt.title(
            wrap_title(
                "Cohen's d for Brain Age Gap (BAG) Between Groups\n"
                "This bar plot shows the standardized difference (Cohen's d) in Brain Age Gap (BAG) between groups. "
                "Larger absolute values indicate larger effect sizes (larger differences in BAG between groups)."
            ),
            fontsize=9,
        )
        plt.xlabel("Cohen's d")
        plt.ylabel("Group Comparison")

        plt.tight_layout()
        cohens_d_plot_path = os.path.join(output_dir, "cohens_d_barplots.png")
        plt.savefig(cohens_d_plot_path)
        plt.close()
        logging.info(f"Cohen's d bar plots saved to: {cohens_d_plot_path}")

        # 2. Box Plots and Statistical Summary
        for metric in ["predicted_age", "brain_age_gap"]:
            plt.figure(figsize=(10, 6))
            sns.boxplot(
                x=group_cols[0],
                y=metric,
                hue=group_cols[1] if len(group_cols) > 1 else None,
                data=data,
            )  # Adapt for single or multiple group_cols
            title_text = (
                f"Box Plot of {metric} Across Groups\n"
                f"This box plot shows the distribution of {metric} for each group.  The box represents the interquartile range (IQR), "
                f"the line inside the box is the median, and the whiskers extend to 1.5 times the IQR.  Outliers are shown as individual points."
            )
            plt.title(wrap_title(title_text), fontsize=9)
            plt.xticks(
                rotation=45, ha="right"
            )  # Rotate x-axis labels for better readability
            plt.tight_layout()
            boxplot_path = os.path.join(output_dir, f"boxplot_{metric}.png")
            plt.savefig(boxplot_path)
            plt.close()
            logging.info(f"Box plot for {metric} saved to: {boxplot_path}")

            # Statistical Summary (Descriptive Statistics)
            stats_summary = data.groupby(group_cols)[metric].describe()
            stats_summary_path = os.path.join(
                output_dir, f"descriptive_stats_{metric}.txt"
            )
            with open(stats_summary_path, "w") as f:
                f.write(stats_summary.to_string())
            logging.info(
                f"Descriptive statistics for {metric} saved to: {stats_summary_path}"
            )

        # 3. Distribution Plots (Histograms and KDE)
        for metric in ["predicted_age", "brain_age_gap"]:
            plt.figure(figsize=(10, 6))
            for group_keys in data.groupby(group_cols).groups.keys():
                group_data = data.loc[
                    data[group_cols].apply(
                        lambda row: tuple(row.values) == group_keys, axis=1
                    ),
                    metric,
                ]
                sns.histplot(
                    group_data,
                    kde=True,
                    label=str(group_keys),
                    stat="density",
                    element="step",
                )  # Use density for better comparison

            title_text = (
                f"Distribution of {metric} Across Groups\n"
                "This plot shows the distribution of {metric} for each group using histograms and kernel density estimates (KDEs). "
                "Overlapping distributions indicate similarity, while distinct distributions suggest differences between groups."
            )
            plt.title(wrap_title(title_text), fontsize=9)
            plt.xlabel(metric)
            plt.ylabel("Density")
            plt.legend()
            plt.tight_layout()
            dist_plot_path = os.path.join(output_dir, f"distribution_{metric}.png")
            plt.savefig(dist_plot_path)
            plt.close()
            logging.info(f"Distribution plot for {metric} saved to: {dist_plot_path}")

        # 4. Violin Plots
        for metric in ["predicted_age", "brain_age_gap"]:

            plt.figure(figsize=(10,6))
            sns.violinplot(x=group_cols[0], y=metric, hue= group_cols[1] if len(group_cols) > 1 else None, data=data, split=True) # adapt for single or multiple group columns
            title_text = f'Violin plot of {metric} Across Groups \n This plot shows a combination of box plot and kernel density estimation. The wider sections of violins represent higher probability density.'
            plt.title(wrap_title(title_text), fontsize=9)
            plt.tight_layout()
            violin_plot_path = os.path.join(output_dir, f"violinplot_{metric}.png")
            plt.savefig(violin_plot_path)
            plt.close()
            logging.info(f"Violin plot saved to: {violin_plot_path}")

    def plot_histograms_and_density(self, data, model_name, model_type):
        """Plots histograms and density plots for predicted age, actual age, and BAG."""
        output_dir = os.path.join(self.output_root, "histogram_density")
        os.makedirs(output_dir, exist_ok=True)
        plt.figure(figsize=(18, 5))
        # Predicted Age
        plt.subplot(1, 3, 1)
        plt.hist(data["predicted_age"], bins=20, alpha=0.7, label="Predicted Age")
        try:
            kde = gaussian_kde(data["predicted_age"])
            x_grid = np.linspace(min(data["predicted_age"]), max(data["predicted_age"]), 1000)
            plt.plot(x_grid, kde(x_grid), color="blue", label="Density (Predicted Age)")
        except np.linalg.LinAlgError as e:
            logging.warning(f"Could not calculate density for predicted age: {e}")
        plt.title(f"{model_name} - Predicted Age")
        plt.xlabel("Age")
        plt.ylabel("Frequency")
        plt.legend()
        # Actual Age
        plt.subplot(1, 3, 2)
        plt.hist(data["actual_age"], bins=20, alpha=0.7, label="Actual Age", color="orange")
        try:
            kde = gaussian_kde(data["actual_age"])
            x_grid = np.linspace(min(data["actual_age"]), max(data["actual_age"]), 1000)
            plt.plot(x_grid, kde(x_grid), color="red", label="Density (Actual Age)")
        except np.linalg.LinAlgError as e:
            logging.warning(f"Could not calculate density for actual age: {e}")
        plt.title("Actual Age")
        plt.xlabel("Age")
        plt.ylabel("Frequency")
        plt.legend()
        # BAG
        plt.subplot(1, 3, 3)
        plt.hist(data["brain_age_gap"], bins=20, alpha=0.7, label="BAG", color="green") # Corrected column name to brain_age_gap
        try:
            kde = gaussian_kde(data["brain_age_gap"]) # Corrected column name to brain_age_gap
            x_grid = np.linspace(min(data["brain_age_gap"]), max(data["brain_age_gap"]), 1000) # Corrected column name to brain_age_gap
            plt.plot(x_grid, kde(x_grid), color="purple", label="Density (BAG)")
        except np.linalg.LinAlgError as e:
            logging.warning(f"Could not calculate density for BAG: {e}")
        plt.title("Brain Age Gap (BAG)")
        plt.xlabel("BAG")
        plt.ylabel("Frequency")
        plt.legend()
        plt.tight_layout()
        output_path = os.path.join(output_dir, f"hist_density.png")
        plt.savefig(output_path)
        plt.close()
        logging.info(f"Histograms and density plots saved to: {output_path}")

    def plot_qq_plots(self, data, model_name, model_type):
        """Creates Q-Q plots for predicted age vs. actual age."""
        output_dir = os.path.join(self.output_root, "qq_plots")
        os.makedirs(output_dir, exist_ok=True)


        # Calculate quantiles
        theoretical_quantiles = np.linspace(0, 1, len(data))
        predicted_age_quantiles = np.quantile(data["predicted_age"], theoretical_quantiles)
        actual_age_quantiles = np.quantile(data["actual_age"], theoretical_quantiles)

        # Q-Q plot
        plt.figure(figsize=(6, 6))
        plt.scatter(actual_age_quantiles, predicted_age_quantiles, alpha=0.7)
        min_val = min(np.min(actual_age_quantiles), np.min(predicted_age_quantiles))
        max_val = max(np.max(actual_age_quantiles), np.max(predicted_age_quantiles))
        plt.plot([min_val, max_val], [min_val, max_val], color="red", linestyle="--") # Identity line
        plt.title(f"{model_name} - Q-Q Plot (Predicted vs. Actual Age)")
        plt.xlabel("Actual Age Quantiles")
        plt.ylabel("Predicted Age Quantiles")
        plt.grid(True)
        output_path = os.path.join(output_dir, f"qq_plot.png")
        plt.savefig(output_path)
        plt.close()
        logging.info(f"Q-Q plot saved to: {output_path}")

    def run_all_analyses(self):
        """Loads models, predicts ages, and performs all analyses."""
        logging.info(f"Number of samples in validation dataset: {len(self.val_dataset)}")
        #val_dataset = [sample for sample in self.val_dataset if sample is not None] # Remove none samples
        val_dataset = self.val_dataset
        logging.info(f"Number of samples after filtering for missing data: {len(val_dataset)}")

        for model_file in os.listdir(self.model_dir):
            if model_file.endswith(".pth"):
                model_path = os.path.join(self.model_dir, model_file)
                try:
                    model, model_type = self.load_model_from_name(model_path)
                    self.output_root = model_path.replace(".pth", "_RESULTS") # Set output root
                    logging.info(f"Loaded model: {model_file}")
                    participant_ids, predicted_ages, actual_ages, demographics_list = self.predict_ages(model, val_dataset) # Get demographics
                    if not participant_ids:
                        logging.warning(f"Skipping model {model_file} due to empty participant_ids after prediction.")
                        continue
                    logging.info(f"Predictions made for model: {model_file}")
                    # Create the DataFrame directly here, ensuring 'participant_id' is included
                    predictions_df = pd.DataFrame({
                        "participant_id": participant_ids,
                        "predicted_age": predicted_ages,
                        "actual_age": actual_ages,
                        "brain_age_gap": np.array(predicted_ages) - np.array(actual_ages)  # Calculate BAG here
                    })

                    # Convert demographics list to DataFrame and concatenate
                    demographics_df = pd.DataFrame(np.array(demographics_list), columns=["Sex", "Site", "LD", "PLD", "Labelling", "Readout"]) # Create demographics DF
                    predictions_df = pd.concat([predictions_df, demographics_df], axis=1) # Concatenate demographics
                    logging.info(f"Demographics added to predictions_df for model: {model_file}")

                    train_participant_ids, train_predicted_ages, train_actual_ages, train_demographics_list = self.predict_ages(model, self.train_dataset)
                    logging.info(f"Predictions made for training data for model: {model_file}")
                    train_predictions_df = pd.DataFrame({
                        "participant_id": train_participant_ids,
                        "predicted_age": train_predicted_ages,
                        "actual_age": train_actual_ages,
                        "brain_age_gap": np.array(train_predicted_ages) - np.array(train_actual_ages) # Calculate BAG for training data
                    })                    
                    # Normative Deviation Analysis
                    logging.info(f"Running normative deviation analysis for model: {model_file}")
                    norm_dev_df = self.calculate_normative_deviations(train_predictions_df, predictions_df, model_file, model_type) # Pass train_predictions_df and predictions_df
                    if 'participant_id' in predictions_df.columns and 'participant_id' in norm_dev_df.columns: #check if 'participant_id' in df before merge
                        predictions_df = pd.merge(predictions_df, norm_dev_df[['participant_id', 'normative_bag']], on='participant_id', how='left') # Merge normative BAGs back into predictions_df
                    logging.info(f"Normative deviations calculated and merged into predictions_df.")
                    norm_dev_stats_df = self.analyze_normative_deviation_stats(norm_dev_df, model_file, model_type) # Analyze normative deviations
                    norm_dev_stats_df.to_csv(os.path.join(self.output_root, f"normative_deviation_stats_{model_file}.csv"), index=False) # Save stats
                    self.plot_normative_deviation_distribution(norm_dev_df, model_file, model_type)

                    # Descriptive Statistics
                    logging.info(f"Running descriptive statistics for model: {model_file}")
                    descriptive_stats_df = self.calculate_descriptive_stats(predictions_df)
                    descriptive_stats_df.to_csv(os.path.join(self.output_root, f"descriptive_stats_{model_file}.csv"), index=False)
                    logging.info(f"Descriptive statistics saved to: {self.output_root}")
                    # Descriptive Statistics by Group
                    if set(self.group_cols).issubset(predictions_df.columns): # Use self.group_cols
                        logging.info(f"Running descriptive statistics by group for model: {model_file}")
                        descriptive_stats_by_group_df = self.calculate_descriptive_stats(predictions_df, group_cols=self.group_cols) # Use self.group_cols
                        descriptive_stats_by_group_df.to_csv(os.path.join(self.output_root, f"descriptive_stats_by_group_{model_file}.csv"), index=False)
                        logging.info(f"Descriptive statistics by group saved to: {self.output_root}")
                        # Calculate effect sizes between groups
                        logging.info(f"Calculating effect sizes for model: {model_file}")
                        effect_sizes_df = self.calculate_effect_sizes(predictions_df, group_cols=self.group_cols) # Use self.group_cols
                        effect_sizes_df.to_csv(os.path.join(self.output_root, f"effect_sizes_{model_file}.csv"), index=False)
                        logging.info(f"Effect sizes saved to: {self.output_root}")
                    else:
                        logging.warning("Skipping descriptive statistics by group and effect size calculation - required columns not found.")

                    # Age Bin Analysis for BAG
                    logging.info(f"Running BAG analysis by age bin for model: {model_file}")
                    bag_by_age_bin_df = self.analyze_bag_by_age_bins(predictions_df, model_file, model_type) # Call new function
                    # Histograms and Density Plots
                    logging.info(f"Creating histograms and density plots for model: {model_file}")
                    self.plot_histograms_and_density(predictions_df, model_file, model_type)
                    # Q-Q Plots
                    logging.info(f"Creating Q-Q plots for model: {model_file}")
                    self.plot_qq_plots(predictions_df, model_file, model_type)
                    # ICC Analysis # Add this section here, after QQ plots for example
                    logging.info(f"Running ICC analysis for model: {model_file}")
                    icc_value, icc_results_df = self.calculate_icc(predictions_df)
                    logging.info(f"ICC Value (ICC2) for {model_file}: {icc_value:.4f}")
                    # CCC Analysis # Add this section after ICC analysis
                    logging.info(f"Running CCC analysis for model: {model_file}")
                    ccc_value = self.calculate_ccc(predictions_df, model_file, model_type)
                    logging.info(f"CCC Value for {model_file}: {ccc_value:.4f}")
                    # Heteroscedasticity Analysis # Add this section after CCC analysis
                    logging.info(f"Running heteroscedasticity analysis for model: {model_file}")
                    levene_stat, levene_p_value = self.analyze_heteroscedasticity(predictions_df, model_file, model_type)
                    # Bias and Variance vs Age Analysis
                    logging.info(f"Running bias and variance vs age analysis for model: {model_file}")
                    bias_variance_df = self.analyze_bias_variance_vs_age(predictions_df, model_file, model_type) # Call new fF_oriunction
                    # Metrics vs Age Plots (Example: MAE vs Age)
                    logging.info(f"Creating metrics vs age plots for model: {model_file}")
                    metrics_vs_age_df = self.plot_metrics_vs_age(predictions_df, model_file, model_type) # Call new function


                except Exception as e:
                    logging.error(f"Error processing model {model_file}:", exc_info=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Brain Age Analysis Script")
    parser.add_argument("--train_csv", type=str, default="/home/radv/samiri/my-scratch/trainingdata/topmri.csv", help="Path to the training CSV file")
    parser.add_argument("--train_img_dir", type=str, default="/home/radv/samiri/my-scratch/trainingdata/topmri/", help="Path to the training image directory")
    parser.add_argument("--val_csv", type=str, default="/home/radv/samiri/my-scratch/testdata/ADC.csv", help="Path to the validation CSV file")
    parser.add_argument("--val_img_dir", type=str, default="/home/radv/samiri/my-scratch/testdata/", help="Path to the validation image directory")
    parser.add_argument("--model_dir", type=str, default="./saved_models", help="Path to the directory containing saved models")
    parser.add_argument("--output_root", type=str, default="analysis_results", help="Root directory for analysis outputs")
    parser.add_argument("--use_cuda", action="store_true", default=False, help="Enable CUDA (GPU) if available")
    parser.add_argument("--group_cols", type=str, default="Sex,Site,Labelling", help="Comma-separated list of columns for group-wise analysis") # Added group_cols argument

    args = parser.parse_args()

    group_cols = [col.strip() for col in args.group_cols.split(',')] # Process group_cols argument

    analyzer = BrainAgeAnalyzer(
        train_csv=args.train_csv,
        train_img_dir=args.train_img_dir,
        val_csv=args.val_csv,
        val_img_dir=args.val_img_dir,
        model_dir=args.model_dir,
        output_root=args.output_root,
        use_cuda=args.use_cuda,
        group_columns=group_cols # Pass group_cols to analyzer
    )
    analyzer.run_all_analyses()